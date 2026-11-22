"""严格加载求职多轮离线脚本；模型脚本与验收条件分开保存。"""

import json
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from .models import EvalCaseError, VALID_EXPECTED_STATUSES, _path_tuple

Json = dict[str, Any]
TOOLS = frozenset(
    {
        "import_career_document",
        "career_memory",
        "career_review",
        "build_resume_draft",
        "export_career_review",
        "web_search",
        "read_web_page",
    }
)
STATUSES = {"succeeded", "failed", "rejected", "skipped", "result_unknown"}
APPROVALS = {
    "career_memory.save",
    "career_memory.forget",
    "career_review.decide",
    "export_career_review",
    "web_search",
    "read_web_page",
}


def obj(value: Any, allowed: set[str], required: set[str], label: str) -> Json:
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise EvalCaseError(f"{label} 缺少必填字段或包含未知字段。")
    return value


def strings(value: Any, label: str) -> None:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise EvalCaseError(f"{label} 必须是非空字符串组成的数组。")


@dataclass(frozen=True)
class CareerCase:
    id: str
    group: str
    description: str
    turns: tuple[Json, ...]

    @classmethod
    def parse(cls, raw: Any) -> "CareerCase":
        data = obj(
            raw,
            {"id", "group", "description", "turns"},
            {"id", "group", "description", "turns"},
            "case",
        )
        if not isinstance(data["id"], str) or not re.fullmatch(r"[a-z0-9_-]+", data["id"]):
            raise EvalCaseError("案例 id 无效。")
        if data["group"] not in ("normal", "challenge") or not isinstance(data["description"], str):
            raise EvalCaseError("案例分组或说明无效。")
        if not isinstance(data["turns"], list) or not 1 <= len(data["turns"]) <= 8:
            raise EvalCaseError("案例需要 1–8 轮。")
        for turn in data["turns"]:
            obj(
                turn,
                {
                    "prompt",
                    "script",
                    "answer",
                    "approvals",
                    "expected",
                    "cancel_before",
                    "fail_model",
                },
                {"prompt", "script", "answer", "approvals", "expected"},
                "turn",
            )
            if (
                not isinstance(turn["prompt"], str)
                or not turn["prompt"].strip()
                or not isinstance(turn["answer"], str)
            ):
                raise EvalCaseError("prompt/answer 必须是文本。")
            for flag in ("cancel_before", "fail_model"):
                if not isinstance(turn.get(flag, False), bool):
                    raise EvalCaseError(f"{flag} 必须是布尔值。")
            if not isinstance(turn["script"], list) or len(turn["script"]) > 12:
                raise EvalCaseError("每轮最多 12 次工具调用。")
            for action in turn["script"]:
                obj(action, {"tool", "arguments", "bind"}, {"tool", "arguments"}, "script")
                if (
                    not isinstance(action["tool"], str)
                    or action["tool"] not in TOOLS
                    or not isinstance(action["arguments"], dict)
                ):
                    raise EvalCaseError("脚本工具或参数无效。")
                if "bind" in action and (
                    not isinstance(action["bind"], str)
                    or not re.fullmatch(r"[a-z_]+", action["bind"])
                ):
                    raise EvalCaseError("绑定名称无效。")
            if not isinstance(turn["approvals"], dict) or any(
                not isinstance(k, str) or not isinstance(v, bool)
                for k, v in turn["approvals"].items()
            ):
                raise EvalCaseError("approvals 必须是操作到布尔值的映射。")
            if set(turn["approvals"]) - APPROVALS:
                raise EvalCaseError("approvals 包含未知操作，不能静默使用默认拒绝。")
            expected = obj(
                turn["expected"],
                {
                    "status",
                    "trace",
                    "state",
                    "files",
                    "network_requests",
                    "forbidden_outbound",
                    "answer_contains",
                },
                {"status", "trace", "state", "files", "network_requests"},
                "expected",
            )
            if (
                not isinstance(expected["status"], str)
                or expected["status"] not in VALID_EXPECTED_STATUSES
            ):
                raise EvalCaseError("期望运行状态无效。")
            if not isinstance(expected["trace"], list) or any(
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(isinstance(value, str) for value in pair)
                or pair[0] not in TOOLS
                or pair[1] not in STATUSES
                for pair in expected["trace"]
            ):
                raise EvalCaseError("期望工具轨迹无效。")
            if type(expected["network_requests"]) is not int or expected["network_requests"] < 0:
                raise EvalCaseError("网络请求数必须是非负整数。")
            if not isinstance(expected["state"], list) or not expected["state"]:
                raise EvalCaseError("必须提供业务状态断言。")
            for assertion in expected["state"]:
                obj(
                    assertion,
                    {"collection", "match", "count", "fields"},
                    {"collection", "match", "count"},
                    "state",
                )
                if (
                    assertion["collection"] not in ("facts", "documents", "reviews")
                    or type(assertion["count"]) is not int
                    or assertion["count"] < 0
                ):
                    raise EvalCaseError("业务集合或计数无效。")
                if not isinstance(assertion["match"], dict) or not isinstance(
                    assertion.get("fields", {}), dict
                ):
                    raise EvalCaseError("状态匹配必须是对象。")
            if not isinstance(expected["files"], dict):
                raise EvalCaseError("files 必须是对象。")
            _path_tuple(list(expected["files"]), "files")
            if any(PureWindowsPath(name).drive for name in expected["files"]):
                raise EvalCaseError("产物路径不能包含 Windows 盘符。")
            for file in expected["files"].values():
                obj(file, {"exists", "contains", "not_contains"}, {"exists"}, "file")
                if not isinstance(file["exists"], bool):
                    raise EvalCaseError("exists 必须是布尔值。")
                strings(file.get("contains", []), "contains")
                strings(file.get("not_contains", []), "not_contains")
            strings(expected.get("forbidden_outbound", []), "forbidden_outbound")
            strings(expected.get("answer_contains", []), "answer_contains")
        return cls(data["id"], data["group"], data["description"], tuple(data["turns"]))


def load_career_cases(path: Path) -> tuple[CareerCase, ...]:
    files = [path] if path.is_file() else sorted(path.glob("*.json"))
    if not files:
        raise EvalCaseError("未找到求职案例。")
    try:
        cases = tuple(CareerCase.parse(json.loads(p.read_text(encoding="utf-8"))) for p in files)
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalCaseError(f"无法加载求职案例：{exc}") from exc
    if len({case.id for case in cases}) != len(cases):
        raise EvalCaseError("求职案例 id 重复。")
    return cases
