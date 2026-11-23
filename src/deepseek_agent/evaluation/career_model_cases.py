"""复用合成材料及业务断言；模型案例不包含离线动作或标准回答。"""

import json
from copy import deepcopy
from pathlib import Path

from ..config import PROJECT_ROOT
from .career_cases import CareerCase, load_career_cases, obj
from .models import EvalCaseError

DEFAULT_MANIFEST = PROJECT_ROOT / "evals/career/model_pilot.json"


def load_model_cases(path: Path = DEFAULT_MANIFEST) -> tuple[CareerCase, ...]:
    try:
        data = obj(
            json.loads(path.read_text(encoding="utf-8")),
            {"version", "cases"},
            {"version", "cases"},
            "model pilot",
        )
    except (OSError, ValueError) as error:
        raise EvalCaseError("无法读取模型评测清单。") from error
    if type(data["version"]) is not int or data["version"] != 1:
        raise EvalCaseError("模型评测清单版本必须为 1。")
    if not isinstance(data["cases"], list) or not 1 <= len(data["cases"]) <= 20:
        raise EvalCaseError("模型评测需要 1–20 个案例。")
    sources = {case.id: case for case in load_career_cases(PROJECT_ROOT / "evals/career/cases")}
    results: list[CareerCase] = []
    seen: set[str] = set()
    for raw in data["cases"]:
        entry = obj(
            raw, {"source_case", "instructions"}, {"source_case", "instructions"}, "pilot case"
        )
        name = entry["source_case"]
        if not isinstance(name, str) or name not in sources or name in seen:
            raise EvalCaseError("模型案例来源无效或重复。")
        seen.add(name)
        source = sources[name]
        instructions = entry["instructions"]
        if (
            not isinstance(instructions, list)
            or len(instructions) != len(source.turns)
            or any(not isinstance(s, str) or not s.strip() or len(s) > 4000 for s in instructions)
        ):
            raise EvalCaseError("每个来源轮次需要一条非空、最多 4000 字符的任务说明。")
        turns = []
        for turn, instruction in zip(source.turns, instructions):
            if turn.get("cancel_before") or turn.get("fail_model"):
                raise EvalCaseError("模型案例不能包含离线故障注入。")
            expected = deepcopy(turn["expected"])
            expected.pop("trace")
            expected.pop("answer_contains", None)
            # 名称可由模型选择；事实内容、版本、状态及产物要求继续检查。
            for assertion in expected["state"]:
                for key in ("fact_key", "title"):
                    assertion["match"].pop(key, None)
            turns.append(
                dict(
                    prompt=turn["prompt"] + "\n\n" + instruction,
                    approvals=deepcopy(turn["approvals"]),
                    expected=expected,
                )
            )
        results.append(CareerCase(source.id, source.group, source.description, tuple(turns)))
    return tuple(results)
