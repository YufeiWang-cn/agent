"""定义本地 Agent 评测案例、检查结果和报告结构。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, cast


class EvalCaseError(ValueError):
    """表示评测案例文件格式无效。"""


def _as_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise EvalCaseError(f"{name} 必须是 JSON 对象。")
    return cast(dict[str, object], value)


def _reject_unknown(mapping: dict[str, object], allowed: set[str], name: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        fields = "、".join(sorted(unknown))
        raise EvalCaseError(f"{name} 包含未知字段：{fields}")


def _required_text(mapping: dict[str, object], key: str, name: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvalCaseError(f"{name}.{key} 必须是非空字符串。")
    return value.strip()


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise EvalCaseError(f"{name} 必须是非空字符串数组。")
    return tuple(cast(list[str], value))


def _path_tuple(value: object, name: str) -> tuple[str, ...]:
    paths = _string_tuple(value, name)
    for path_text in paths:
        path = PurePosixPath(path_text.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or path_text in {"", "."}:
            raise EvalCaseError(f"{name} 只能包含工作区内的相对文件路径。")
    return tuple(path.replace("\\", "/") for path in paths)


def _positive_int(value: object, default: int, name: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise EvalCaseError(f"{name} 必须是正整数。")
    return value


def _nonnegative_int(value: object, default: int, name: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EvalCaseError(f"{name} 必须是非负整数。")
    return value


def _int_tuple(value: object, name: str) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in value
    ):
        raise EvalCaseError(f"{name} 必须是整数数组。")
    return tuple(cast(list[int], value))


def _optional_directory(
    mapping: dict[str, object],
    key: str,
    base_directory: Path,
) -> Path | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EvalCaseError(f"案例.{key} 必须是非空字符串。")
    directory = (base_directory / value).resolve(strict=False)
    if not directory.is_dir():
        raise EvalCaseError(f"案例.{key} 不是可用目录：{directory}")
    return directory


@dataclass(frozen=True, slots=True)
class EvalCommand:
    """描述 Agent 结束后由评测器执行的一条无 Shell 验证命令。"""

    argv: tuple[str, ...]
    expected_exit_code: int = 0
    timeout_seconds: float = 120.0

    @classmethod
    def from_dict(cls, value: object, name: str) -> "EvalCommand":
        mapping = _as_mapping(value, name)
        _reject_unknown(
            mapping,
            {"argv", "expected_exit_code", "timeout_seconds"},
            name,
        )
        argv = _string_tuple(mapping.get("argv"), f"{name}.argv")
        if not argv:
            raise EvalCaseError(f"{name}.argv 不能为空。")
        exit_code = mapping.get("expected_exit_code", 0)
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise EvalCaseError(f"{name}.expected_exit_code 必须是整数。")
        timeout_value = mapping.get("timeout_seconds", 120.0)
        if not isinstance(timeout_value, (int, float)) or isinstance(
            timeout_value, bool
        ):
            raise EvalCaseError(f"{name}.timeout_seconds 必须是正数。")
        timeout_seconds = float(timeout_value)
        if timeout_seconds <= 0:
            raise EvalCaseError(f"{name}.timeout_seconds 必须大于 0。")
        return cls(argv, exit_code, timeout_seconds)


@dataclass(frozen=True, slots=True)
class EvalCase:
    """保存一道端到端 Agent 评测题及其确定性验收条件。"""

    id: str
    prompt: str
    fixture: Path
    grader_fixture: Path | None = None
    outside_fixture: Path | None = None
    approval_policy: str = "deny"
    expected_statuses: tuple[str, ...] = ("completed",)
    required_files_changed: tuple[str, ...] = ()
    allowed_files_changed: tuple[str, ...] | None = None
    forbidden_files_changed: tuple[str, ...] = ()
    commands: tuple[EvalCommand, ...] = ()
    required_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    min_rejected_calls: int = 0
    required_command_exit_sequence: tuple[int, ...] = ()
    answer_required_substrings: tuple[str, ...] = ()
    answer_forbidden_substrings: tuple[str, ...] = ()
    allow_side_effects: bool = False
    max_tool_calls: int = 20
    max_identical_tool_calls: int = 3

    @property
    def approve_tools(self) -> bool:
        """返回本案例是否统一批准需要确认的工具。"""
        return self.approval_policy == "allow"

    @classmethod
    def from_file(cls, path: Path) -> "EvalCase":
        """读取并严格校验一个 JSON 案例文件。"""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise EvalCaseError(f"无法读取评测案例 {path}：{error}") from error
        return cls.from_dict(_as_mapping(raw, str(path)), base_directory=path.parent)

    @classmethod
    def from_dict(
        cls,
        mapping: dict[str, object],
        *,
        base_directory: Path,
    ) -> "EvalCase":
        """从已经解析的 JSON 对象构造案例。"""
        _reject_unknown(
            mapping,
            {
                "id", "prompt", "fixture", "grader_fixture", "outside_fixture",
                "approval_policy", "expected", "limits",
            },
            "案例",
        )
        case_id = _required_text(mapping, "id", "案例")
        allowed_id_chars = "abcdefghijklmnopqrstuvwxyz0123456789-_"
        if any(character not in allowed_id_chars for character in case_id):
            raise EvalCaseError("案例.id 只能包含小写字母、数字、连字符和下划线。")
        prompt = _required_text(mapping, "prompt", "案例")
        fixture_text = _required_text(mapping, "fixture", "案例")
        fixture = (base_directory / fixture_text).resolve(strict=False)
        if not fixture.is_dir():
            raise EvalCaseError(f"案例.fixture 不是可用目录：{fixture}")

        approval_policy = mapping.get("approval_policy", "deny")
        if approval_policy not in {"allow", "deny"}:
            raise EvalCaseError("案例.approval_policy 只能是 allow 或 deny。")
        expected = _as_mapping(mapping.get("expected", {}), "案例.expected")
        _reject_unknown(
            expected,
            {
                "statuses", "required_files_changed", "allowed_files_changed",
                "forbidden_files_changed", "commands", "required_tools",
                "forbidden_tools", "answer_required_substrings",
                "answer_forbidden_substrings", "allow_side_effects",
                "min_rejected_calls", "required_command_exit_sequence",
            },
            "案例.expected",
        )
        statuses = _string_tuple(
            expected.get("statuses", ["completed"]), "案例.expected.statuses"
        )
        if not statuses:
            raise EvalCaseError("案例.expected.statuses 不能为空。")
        allowed_files = (
            None
            if "allowed_files_changed" not in expected
            else _path_tuple(
                expected.get("allowed_files_changed"),
                "案例.expected.allowed_files_changed",
            )
        )
        command_values = expected.get("commands", [])
        if not isinstance(command_values, list):
            raise EvalCaseError("案例.expected.commands 必须是数组。")
        commands = tuple(
            EvalCommand.from_dict(item, f"案例.expected.commands[{index}]")
            for index, item in enumerate(command_values)
        )
        allow_side_effects = expected.get("allow_side_effects", False)
        if not isinstance(allow_side_effects, bool):
            raise EvalCaseError("案例.expected.allow_side_effects 必须是布尔值。")
        limits = _as_mapping(mapping.get("limits", {}), "案例.limits")
        _reject_unknown(
            limits, {"max_tool_calls", "max_identical_tool_calls"}, "案例.limits"
        )
        return cls(
            id=case_id,
            prompt=prompt,
            fixture=fixture,
            grader_fixture=_optional_directory(
                mapping, "grader_fixture", base_directory
            ),
            outside_fixture=_optional_directory(
                mapping, "outside_fixture", base_directory
            ),
            approval_policy=approval_policy,
            expected_statuses=statuses,
            required_files_changed=_path_tuple(
                expected.get("required_files_changed"),
                "案例.expected.required_files_changed",
            ),
            allowed_files_changed=allowed_files,
            forbidden_files_changed=_path_tuple(
                expected.get("forbidden_files_changed"),
                "案例.expected.forbidden_files_changed",
            ),
            commands=commands,
            required_tools=_string_tuple(
                expected.get("required_tools"), "案例.expected.required_tools"
            ),
            forbidden_tools=_string_tuple(
                expected.get("forbidden_tools"), "案例.expected.forbidden_tools"
            ),
            min_rejected_calls=_nonnegative_int(
                expected.get("min_rejected_calls"),
                0,
                "案例.expected.min_rejected_calls",
            ),
            required_command_exit_sequence=_int_tuple(
                expected.get("required_command_exit_sequence"),
                "案例.expected.required_command_exit_sequence",
            ),
            answer_required_substrings=_string_tuple(
                expected.get("answer_required_substrings"),
                "案例.expected.answer_required_substrings",
            ),
            answer_forbidden_substrings=_string_tuple(
                expected.get("answer_forbidden_substrings"),
                "案例.expected.answer_forbidden_substrings",
            ),
            allow_side_effects=allow_side_effects,
            max_tool_calls=_positive_int(
                limits.get("max_tool_calls"), 20, "案例.limits.max_tool_calls"
            ),
            max_identical_tool_calls=_positive_int(
                limits.get("max_identical_tool_calls"),
                3,
                "案例.limits.max_identical_tool_calls",
            ),
        )


@dataclass(frozen=True, slots=True)
class CheckResult:
    """保存一个确定性检查器的结果。"""

    name: str
    passed: bool
    hard_gate: bool
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "hard_gate": self.hard_gate,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class EvalResult:
    """保存一道评测题的完整结果和运行指标。"""

    case_id: str
    passed: bool
    checks: tuple[CheckResult, ...]
    metrics: dict[str, Any]
    error: str | None = None
    failed_workspace: str | None = None
    attempt: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "attempt": self.attempt,
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
            "metrics": self.metrics,
            "error": self.error,
            "failed_workspace": self.failed_workspace,
        }


def load_cases(path: Path) -> tuple[EvalCase, ...]:
    """从单个 JSON 文件或目录递归加载案例。"""
    if path.is_file():
        return (EvalCase.from_file(path),)
    if not path.is_dir():
        raise EvalCaseError(f"评测案例路径不存在：{path}")
    case_files = sorted(path.rglob("*.json"))
    if not case_files:
        raise EvalCaseError(f"评测案例目录中没有 JSON 文件：{path}")
    cases = tuple(EvalCase.from_file(case_path) for case_path in case_files)
    identifiers = [case.id for case in cases]
    if len(set(identifiers)) != len(identifiers):
        raise EvalCaseError("评测案例 id 不能重复。")
    return cases


__all__ = [
    "CheckResult",
    "EvalCase",
    "EvalCaseError",
    "EvalCommand",
    "EvalResult",
    "load_cases",
]
