"""对照报告与成对人工评分；不使用模型或关键词伪造语义分数。"""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

DIMENSIONS = {
    "faithfulness": ["虚构职责、指标或经历", "轻微夸大或归属模糊", "忠实于来源并区分本人贡献"],
    "gap_identification": ["编造代替追问", "发现缺口但问题笼统", "明确指出缺口并提出可回答问题"],
    "actionability": [
        "与目标无关或无法操作",
        "有方向但缺少具体修改",
        "建议具体且说明证据或验证方式",
    ],
    "clarity": ["容易误解", "基本清楚但冗长", "清晰区分事实、建议和能力边界"],
}
ARMS = ("agent", "baseline")


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _pairs(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = {
        (r["case_id"], r["attempt"], r["metrics"].get("arm", "agent")): r for r in report["results"]
    }
    pairs = []
    for task in report["plan"]["tasks"]:
        for attempt in range(1, report["plan"].get("repeat", 1) + 1):
            members = {arm: rows.get((task["id"], attempt, arm)) for arm in ARMS}
            reasons = []
            for arm, row in members.items():
                observations = row["metrics"].get("observations", []) if row else []
                if row is None:
                    reasons.append(f"{arm}:not_run")
                elif len(observations) != len(task["prompts"]) or any(
                    o["status"] not in {"completed", "waiting_user"} or not o["answer"].strip()
                    for o in observations
                ):
                    reasons.append(f"{arm}:incomplete")
            pairs.append(
                dict(
                    case_id=task["id"],
                    attempt=attempt,
                    eligible=not reasons,
                    reasons=reasons,
                    members=members,
                    prompts=task["prompts"],
                )
            )
    return pairs


def comparison_summary(report: dict[str, Any]) -> dict[str, Any]:
    pairs = _pairs(report)
    arm_metrics = {}
    for arm in ARMS:
        rows = [r for r in report["results"] if r["metrics"].get("arm", "agent") == arm]
        metrics = [r["metrics"] for r in rows]
        arm_metrics[arm] = dict(
            runs=len(rows),
            checks_passed=sum(r["passed"] for r in rows),
            pass_meaning="business_workflow" if arm == "agent" else "response_protocol_only",
            provider_attempts=sum(m.get("provider_attempts", 0) for m in metrics),
            input_tokens=sum(m.get("input_tokens", 0) for m in metrics),
            output_tokens=sum(m.get("output_tokens", 0) for m in metrics),
            usage_unknown_attempts=sum(m.get("usage_unknown_attempts", 0) for m in metrics),
            duration_seconds=sum(m.get("duration_seconds", 0) for m in metrics),
            failure_categories={
                name: sum(
                    not c["passed"] and c["name"].endswith("." + name)
                    for r in rows
                    for c in r["checks"]
                )
                for name in (
                    "termination",
                    "business_state",
                    "artifacts",
                    "privacy",
                    "confirmation",
                )
            },
        )
    return dict(
        pairs=[{k: p[k] for k in ("case_id", "attempt", "eligible", "reasons")} for p in pairs],
        eligible_pairs=sum(p["eligible"] for p in pairs),
        arm_metrics=arm_metrics,
        semantic_review="not_run",
        quality_delta=None,
        scope="系统整体对照，不是等算力或单一提示词消融；只提供描述性结果。",
    )


def _samples(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if report["plan"].get("arms") != list(ARMS) or not report.get("blinding_seed"):
        raise ValueError("请使用带 --compare 的两组报告。")
    samples = {}
    for pair in _pairs(report):
        if not pair["eligible"]:
            continue
        for arm in ARMS:
            row = pair["members"][arm]
            sample_id = _digest([report["blinding_seed"], pair["case_id"], pair["attempt"], arm])[
                :24
            ]
            outputs = [
                dict(turn=index, answer=o["answer"], artifacts=o.get("artifacts", {}))
                for index, o in enumerate(row["metrics"]["observations"], 1)
            ]
            samples[sample_id] = dict(
                case_id=pair["case_id"],
                attempt=pair["attempt"],
                arm=arm,
                prompts=pair["prompts"],
                outputs=outputs,
            )
    return samples


def make_review_sheet(report: dict[str, Any]) -> dict[str, Any]:
    samples = _samples(report)
    if not samples:
        raise ValueError("没有完整的成对输出，不能生成评分表。")
    return dict(
        version=1,
        report_sha256=_digest(report),
        rubric=DIMENSIONS,
        items=[
            dict(
                sample_id=key,
                case_id=value["case_id"],
                attempt=value["attempt"],
                prompts=value["prompts"],
                outputs=value["outputs"],
                ratings={
                    dimension: dict(score=None, input_quote="", output_quote="", reason="")
                    for dimension in DIMENSIONS
                },
                fabricated_claim=dict(present=None, input_quote="", output_quote="", reason=""),
            )
            for key, value in sorted(samples.items())
        ],
        note="隐藏组名并打乱呈现顺序；输出格式仍可能暴露组别。不填写即未评分，不能把模板当作结果。",
    )


def _evidence(value: dict[str, Any], sample: dict[str, Any]) -> None:
    inputs = "\n".join(sample["prompts"])
    outputs = "\n".join(
        [o["answer"] for o in sample["outputs"]]
        + [text for o in sample["outputs"] for text in o["artifacts"].values()]
    )
    for field, source in (("input_quote", inputs), ("output_quote", outputs)):
        quote = value.get(field)
        if not isinstance(quote, str) or not quote.strip() or quote not in source:
            raise ValueError(f"{field} 必须逐字引用该样本的输入或输出。")
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        raise ValueError("评分必须填写理由。")


def score_reviews(report: dict[str, Any], sheet: dict[str, Any]) -> dict[str, Any]:
    template = make_review_sheet(report)
    if (
        set(sheet) != set(template)
        or type(sheet["version"]) is not int
        or sheet["version"] != 1
        or sheet["report_sha256"] != template["report_sha256"]
        or sheet["rubric"] != DIMENSIONS
    ):
        raise ValueError("评分表版本、量表或报告摘要不匹配。")
    samples = _samples(report)
    if not isinstance(sheet["items"], list) or len(sheet["items"]) != len(samples):
        raise ValueError("必须完整评分全部成对样本，不能删除不利样本。")
    expected_items = {item["sample_id"]: item for item in template["items"]}
    rated: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    seen = set()
    flags = []
    for item in sheet["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("sample_id"), str):
            raise ValueError("评分样本无效。")
        key = item["sample_id"]
        if key not in samples or key in seen or set(item) != set(expected_items[key]):
            raise ValueError("评分样本未知、重复或字段不完整。")
        seen.add(key)
        for field in ("case_id", "attempt", "prompts", "outputs"):
            if item[field] != expected_items[key][field]:
                raise ValueError("不能修改评分表中的原始材料或模型输出。")
        ratings = item["ratings"]
        if not isinstance(ratings, dict) or set(ratings) != set(DIMENSIONS):
            raise ValueError("必须评分全部四个维度。")
        sample = samples[key]
        scores = {}
        for dimension, value in ratings.items():
            if (
                not isinstance(value, dict)
                or set(value) != {"score", "input_quote", "output_quote", "reason"}
                or type(value["score"]) is not int
                or value["score"] not in (0, 1, 2)
            ):
                raise ValueError("每个维度需要 0–2 的整数分及证据，不能自动填分。")
            _evidence(value, sample)
            scores[dimension] = value["score"]
        claim = item["fabricated_claim"]
        if (
            not isinstance(claim, dict)
            or set(claim) != {"present", "input_quote", "output_quote", "reason"}
            or not isinstance(claim["present"], bool)
        ):
            raise ValueError("必须明确检查是否虚构个人成果。")
        _evidence(claim, sample)
        if claim["present"]:
            flags.append(
                dict(
                    sample_id=key,
                    case_id=sample["case_id"],
                    attempt=sample["attempt"],
                    arm=sample["arm"],
                    evidence=claim,
                )
            )
        rated.setdefault((sample["case_id"], sample["attempt"]), {})[sample["arm"]] = scores
    pairs: list[dict[str, Any]] = [
        dict(
            case_id=key[0],
            attempt=key[1],
            scores=value,
            delta={d: value["agent"][d] - value["baseline"][d] for d in DIMENSIONS},
        )
        for key, value in sorted(rated.items())
    ]
    return dict(
        status="complete",
        report_sha256=_digest(report),
        pairs=pairs,
        mean_paired_delta={d: sum(p["delta"][d] for p in pairs) / len(pairs) for d in DIMENSIONS},
        fabricated_claims=flags,
        annotation_sheet=sheet,
        claim="仅所评分样本的描述性差异；不是统计显著性结论，虚构成果需单独审查。",
        evidence_mode=report.get("evidence_mode"),
    )


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="导出匿名评分表，或校验人工评分并生成成对结果；不调用模型。"
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--annotations", type=Path)
    options = parser.parse_args(argv)
    try:
        report = json.loads(options.report.read_text(encoding="utf-8"))
        result = (
            score_reviews(report, json.loads(options.annotations.read_text(encoding="utf-8")))
            if options.annotations
            else make_review_sheet(report)
        )
        options.output.parent.mkdir(parents=True, exist_ok=True)
        with options.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(f"已保存：{options.output}")
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
