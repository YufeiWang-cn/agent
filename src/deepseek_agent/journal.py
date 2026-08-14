import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .models import ToolCallRequest
from .tool_execution import ToolExecutionRecord, ToolExecutionStart


JOURNAL_VERSION = 1


class RunJournalError(RuntimeError):
    """表示运行日志无法可靠读取或写入。"""


@dataclass(frozen=True, slots=True)
class RecoveryIssue:
    """描述上次运行中已经开始但没有明确结束记录的工具调用。"""

    run_id: str
    turn_id: str
    call_id: str
    tool_name: str
    effect: str
    started_at: str
    argument_digest: str
    tool_status: str | None = None

    @property
    def message(self) -> str:
        """返回适合直接展示给用户的恢复提示。"""
        if self.tool_status == "succeeded":
            return (
                f"工具 {self.tool_name}（调用 {self.call_id}）已经明确执行成功，"
                "但所属轮次没有完成保存。外部状态可能已经改变，而会话记录可能尚未同步，"
                "请先检查实际状态。"
            )
        if self.tool_status == "result_unknown":
            return (
                f"工具 {self.tool_name}（调用 {self.call_id}）已经执行，"
                "但工具报告结果未知，并且所属轮次没有完成保存。"
                "请先检查外部状态，不要直接自动重试。"
            )
        return (
            f"工具 {self.tool_name}（调用 {self.call_id}）已经开始执行，"
            "但没有找到结束记录，因此实际结果未知。请先检查外部状态，"
            "不要直接自动重试。"
        )


Clock = Callable[[], datetime]


class RunJournal:
    """使用只追加 JSONL 文件保存 Agent 的关键执行事件。"""

    def __init__(
        self,
        directory: Path,
        *,
        run_id: str | None = None,
        clock: Clock | None = None,
        durable: bool = True,
    ) -> None:
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._run_id = run_id or uuid4().hex
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._durable = durable
        self._lock = threading.Lock()

    @property
    def run_id(self) -> str:
        """返回当前 Agent 进程使用的运行标识。"""
        return self._run_id

    @property
    def directory(self) -> Path:
        """返回保存 JSONL 运行日志的目录。"""
        return self._directory

    @property
    def path(self) -> Path:
        """返回当前运行对应的 JSONL 文件路径。"""
        return self._directory / f"{self._run_id}.jsonl"

    def record_turn_started(self, turn_id: str, session_id: str) -> None:
        """记录一轮对话已经开始，但不保存用户的提示词。"""
        self._append(
            "turn_started",
            turn_id=turn_id,
            session_id=session_id,
        )

    def record_confirmation_requested(
        self,
        turn_id: str,
        request: ToolCallRequest,
    ) -> None:
        """记录工具正在等待用户确认，但不保存原始参数。"""
        self._append(
            "confirmation_requested",
            turn_id=turn_id,
            call_id=request.id,
            tool_name=request.name,
            arguments=_summarize_arguments(request.arguments, None),
        )

    def record_tool_started(
        self,
        turn_id: str,
        start: ToolExecutionStart,
    ) -> None:
        """在工具真正执行前持久化调用身份和安全策略。"""
        self._append(
            "tool_started",
            turn_id=turn_id,
            call_id=start.call_id,
            tool_name=start.tool_name,
            effect=start.effect.value,
            retryable=start.retryable,
            idempotent=start.idempotent,
            supports_rollback=start.supports_rollback,
            arguments=_summarize_arguments(
                start.raw_arguments,
                start.arguments,
            ),
        )

    def record_tool_finished(
        self,
        turn_id: str,
        record: ToolExecutionRecord,
    ) -> None:
        """记录工具的明确结果，使开始事件不再被视为悬空状态。"""
        self._append(
            "tool_finished",
            turn_id=turn_id,
            call_id=record.call_id,
            tool_name=record.tool_name,
            status=record.status.value,
            effect=record.effect.value,
            execution_started=record.execution_started,
            may_have_side_effect=record.may_have_side_effect,
            can_retry_safely=record.can_retry_safely,
            duration_ms=round(record.duration_seconds * 1000, 3),
            error_present=record.error_message is not None,
        )

    def record_turn_finished(
        self,
        turn_id: str,
        *,
        status: str,
        steps_completed: int,
        tool_calls_completed: int,
        history_preserved: bool,
        error_present: bool,
    ) -> None:
        """记录一轮对话的终止状态，但不保存回答正文或异常正文。"""
        self._append(
            "turn_finished",
            turn_id=turn_id,
            status=status,
            steps_completed=steps_completed,
            tool_calls_completed=tool_calls_completed,
            history_preserved=history_preserved,
            error_present=error_present,
        )

    def find_recovery_issues(self) -> tuple[RecoveryIssue, ...]:
        """扫描日志并返回没有被已保存轮次闭合的副作用工具调用。"""
        pending: dict[tuple[str, str, str], dict[str, Any]] = {}
        try:
            paths = sorted(self._directory.glob("*.jsonl"))
        except OSError as error:
            raise RunJournalError(f"无法扫描运行日志目录：{error}") from error

        for path in paths:
            try:
                stream = path.open("r", encoding="utf-8")
            except OSError as error:
                raise RunJournalError(f"无法读取运行日志 {path.name}：{error}") from error
            with stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        # 进程可能在追加最后一行时被强制终止，因此恢复扫描会忽略损坏的行。
                        continue
                    if not isinstance(event, dict):
                        continue
                    event_type = event.get("event")
                    if event_type == "turn_finished":
                        run_id = event.get("run_id")
                        turn_id = event.get("turn_id")
                        for pending_key in tuple(pending):
                            if pending_key[:2] == (run_id, turn_id):
                                pending.pop(pending_key, None)
                        continue
                    key = _tool_event_key(event)
                    if key is None:
                        continue
                    if event_type == "tool_started":
                        if event.get("effect") != "read_only":
                            pending[key] = event
                    elif event_type == "tool_finished":
                        if event.get("may_have_side_effect"):
                            started = pending.get(key, {})
                            pending[key] = {
                                **event,
                                "timestamp": started.get(
                                    "timestamp",
                                    event.get("timestamp", ""),
                                ),
                                "arguments": started.get("arguments", {}),
                                "tool_status": event.get("status"),
                            }
                        else:
                            pending.pop(key, None)

        issues = [
            RecoveryIssue(
                run_id=str(event.get("run_id", "")),
                turn_id=str(event.get("turn_id", "")),
                call_id=str(event.get("call_id", "")),
                tool_name=str(event.get("tool_name", "未知工具")),
                effect=str(event.get("effect", "unknown")),
                started_at=str(event.get("timestamp", "")),
                argument_digest=str(
                    (event.get("arguments") or {}).get("sha256", "")
                ),
                tool_status=(
                    str(event["tool_status"])
                    if event.get("tool_status") is not None
                    else None
                ),
            )
            for event in pending.values()
        ]
        return tuple(sorted(issues, key=lambda item: item.started_at))

    def _append(self, event_type: str, **payload: Any) -> None:
        """以单行形式追加事件，并在需要时强制刷新到磁盘。"""
        event = {
            "version": JOURNAL_VERSION,
            "timestamp": self._clock().isoformat(),
            "event": event_type,
            "run_id": self._run_id,
            **payload,
        }
        serialized = json.dumps(
            event,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            with self._lock:
                with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(serialized + "\n")
                    stream.flush()
                    if self._durable:
                        os.fsync(stream.fileno())
        except OSError as error:
            raise RunJournalError(f"无法写入运行日志：{error}") from error


def _summarize_arguments(
    raw_arguments: str,
    arguments: dict[str, Any] | None,
) -> dict[str, Any]:
    """返回不包含参数值的键名、大小和摘要。"""
    encoded = raw_arguments.encode("utf-8")
    keys = sorted(str(key) for key in arguments) if arguments is not None else []
    if arguments is None:
        try:
            parsed = json.loads(raw_arguments or "{}")
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            keys = sorted(str(key) for key in parsed)
    return {
        "keys": keys,
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _tool_event_key(event: dict[str, Any]) -> tuple[str, str, str] | None:
    """返回用于匹配工具开始与结束事件的稳定键。"""
    if event.get("event") not in {"tool_started", "tool_finished"}:
        return None
    values = (
        event.get("run_id"),
        event.get("turn_id"),
        event.get("call_id"),
    )
    if not all(isinstance(value, str) and value for value in values):
        return None
    return values


__all__ = ["RecoveryIssue", "RunJournal", "RunJournalError"]
