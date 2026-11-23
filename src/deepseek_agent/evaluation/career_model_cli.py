"""求职模型评测：默认仅预览；显式执行时隔离档案、共享预算并逐轮保存。"""

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from ..config import PROJECT_ROOT, Settings
from ..context.estimator import estimate_messages_tokens, estimate_text_tokens
from ..conversation import Message
from ..models import ChatModel, DeepSeekModel, StreamEvent
from ..reliability import ModelBudget, ModelCallError
from .career_cases import CareerCase
from .career_model_cases import DEFAULT_MANIFEST, load_model_cases
from .career_baseline import CareerBaselineRunner
from .career_comparison import comparison_summary
from .career_runner import CareerEvalRunner
from .models import CheckResult, EvalCaseError, EvalResult

SYSTEM_PROMPT = "你是求职助手。本次材料为合成材料，请完成用户明确要求，不虚构经历或成绩。"
ModelFactory = Callable[[Settings], ChatModel]


class CheckedModel:
    """在供应商适配器之前预检完整消息和工具 schema；估算不代表精确计费。"""

    def __init__(self, model: ChatModel, max_input_tokens: int) -> None:
        self.model = model
        self.max_input_tokens = max_input_tokens
        self.provider_attempts = 0
        self.input_rejections = 0
        self.max_observed_input_estimate = 0

    @property
    def model_name(self) -> str:
        return self.model.model_name

    def stream(
        self, messages: Sequence[Message], tools: Sequence[dict[str, Any]]
    ) -> Iterable[StreamEvent]:
        estimate = estimate_messages_tokens(list(messages)) + estimate_text_tokens(
            json.dumps(list(tools), ensure_ascii=False, separators=(",", ":"))
        )
        self.max_observed_input_estimate = max(self.max_observed_input_estimate, estimate)
        if estimate > self.max_input_tokens:
            self.input_rejections += 1
            raise ModelCallError(
                "完整请求的估算输入超过评测上限。",
                category="input_limit",
                retryable=False,
                partial=False,
            )
        self.provider_attempts += 1
        yield from self.model.stream(messages, tools)


def _source_digest() -> str:
    root = Path(__file__).parents[1]
    digest = hashlib.sha256()
    for path in sorted([*root.rglob("*.py"), *root.rglob("*.md")]):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _checkpoint(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + f".{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run(argv: list[str] | None = None, *, model_factory: ModelFactory | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--compare", action="store_true", help="完整 Agent 与无工具直接提示词基线共用总预算"
    )
    parser.add_argument("--execute", action="store_true", help="显式发起模型调用；默认仅预览")
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-model-calls", type=int, default=120)
    parser.add_argument("--max-calls-per-turn", type=int, default=6)
    parser.add_argument(
        "--max-input-tokens", type=int, default=12000, help="完整请求的估算输入上限"
    )
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument(
        "--output", type=Path, default=PROJECT_ROOT / "data/evals/career-model.json"
    )
    options = parser.parse_args(argv)
    if not 1 <= options.repeat <= 10:
        parser.error("repeat 必须在 1–10 之间。")
    if (
        min(
            options.max_model_calls,
            options.max_calls_per_turn,
            options.max_input_tokens,
            options.max_output_tokens,
        )
        < 1
    ):
        parser.error("调用与 token 上限必须为正整数。")
    url = urlsplit(options.base_url)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or not options.model.strip()
    ):
        parser.error("模型名不能为空，base-url 必须为不含凭据、查询或片段的 HTTPS 地址。")
    try:
        cases = load_model_cases(options.manifest)
    except EvalCaseError as error:
        parser.error(str(error))
    original_turns = sum(len(case.turns) for case in cases) * options.repeat
    arms = ["agent", "baseline"] if options.compare else ["agent"]
    turns = original_turns * len(arms)
    ceiling = min(
        options.max_model_calls,
        original_turns * (options.max_calls_per_turn + int(options.compare)),
    )
    plan: dict[str, Any] = dict(
        mode="preview",
        cases=len(cases),
        attempts=len(cases) * options.repeat * len(arms),
        repeat=options.repeat,
        arms=arms,
        baseline_calls_per_turn=1 if options.compare else None,
        user_turns=turns,
        model=options.model,
        base_url=options.base_url,
        batch_call_limit=options.max_model_calls,
        calls_per_turn=options.max_calls_per_turn,
        effective_call_ceiling=ceiling,
        retries=0,
        estimated_input_limit=options.max_input_tokens,
        output_token_limit=options.max_output_tokens,
        token_limits_are_not_a_money_guarantee=True,
        network="fixed-synthetic-snapshot",
        real_api_calls=0,
        output=str(options.output),
        source_sha256=_source_digest(),
        cases_sha256=hashlib.sha256(
            json.dumps([asdict(c) for c in cases], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        tasks=[
            dict(
                id=c.id,
                prompts=[t["prompt"] for t in c.turns],
                approvals=[t["approvals"] for t in c.turns],
            )
            for c in cases
        ],
        scope=(
            "同模型和用户材料的系统整体对照；人工评分前不报告质量提升。"
            if options.compare
            else "仅完整 Agent 流程；未执行提示词基线或人工语义评分。"
        ),
    )
    if not options.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    if options.output.exists():
        parser.error("报告已存在，请使用新的 output 路径；不会覆盖旧报告。")
    # 注入只用于本地测试；它不被当作真实供应商证据。
    if model_factory is None:
        settings = Settings.from_env()
        factory: ModelFactory = DeepSeekModel
    else:
        settings = Settings("injected", options.base_url, options.model, SYSTEM_PROMPT)
        factory = model_factory
    settings = replace(
        settings,
        model=options.model,
        base_url=options.base_url,
        system_prompt=SYSTEM_PROMPT,
        max_retries=0,
        max_model_calls_per_turn=options.max_calls_per_turn,
        max_output_tokens=options.max_output_tokens,
        max_context_tokens=min(8000, options.max_input_tokens),
        agent_mode="career",
        career_profile_id="eval",
    )
    budget = ModelBudget(options.max_model_calls)
    runner = CareerEvalRunner(model_budget=budget)
    baseline_runner = CareerBaselineRunner(budget)
    scheduled: list[tuple[CareerCase, int, str]] = []
    for case in cases:
        for attempt in range(1, options.repeat + 1):
            # 成对交错，轮换先运行的组；额度截停不能把未执行组当成零分。
            order = arms if (len(scheduled) // len(arms)) % 2 == 0 else list(reversed(arms))
            scheduled.extend((case, attempt, arm) for arm in order)
    results: list[dict[str, Any]] = []
    active_models: list[CheckedModel] = []
    report: dict[str, Any] = dict(
        plan=plan,
        evidence_mode="real-model" if model_factory is None else "injected-model",
        status="running",
        results=results,
        in_progress=None,
        not_run=[],
        human_review=dict(status="not_run"),
        baseline=dict(status="not_run"),
        blinding_seed=uuid4().hex if options.compare else None,
        business_checks_are_not_semantic_quality=True,
    )

    def save() -> None:
        attempts = sum(model.provider_attempts for model in active_models)
        report.update(
            model_budget=budget.snapshot(),
            provider_attempts=attempts,
            real_api_calls=attempts if model_factory is None else None,
            passed=sum(r["passed"] for r in results),
            failure_categories=dict(
                Counter(
                    c["name"].split(".")[-1]
                    for r in results
                    for c in r["checks"]
                    if not c["passed"] and c["hard_gate"]
                )
            ),
        )
        if options.compare:
            report["comparison"] = comparison_summary(report)
            baseline_count = sum(r["metrics"].get("arm") == "baseline" for r in results)
            report["baseline"] = dict(
                status=(
                    "complete"
                    if baseline_count == len(cases) * options.repeat
                    else "partial"
                    if baseline_count
                    else "not_run"
                )
            )
        _checkpoint(options.output, report)

    def progress(value: dict[str, Any]) -> None:
        report["in_progress"] = dict(value, arm=current_arm)
        save()

    options.output.parent.mkdir(parents=True, exist_ok=True)
    # 排他创建，随后只替换本次运行自己的报告；不静默覆盖已有实验。
    with options.output.open("x", encoding="utf-8") as stream:
        stream.write("{}")
    save()
    try:
        for index, (case, attempt, current_arm) in enumerate(scheduled):
            if budget.snapshot()["remaining"] == 0:
                report["status"] = "budget_exhausted"
                break
            report["in_progress"] = dict(
                case_id=case.id, attempt=attempt, arm=current_arm, observations=[]
            )
            save()
            model: CheckedModel | None = None
            try:
                model = CheckedModel(factory(settings), options.max_input_tokens)
                active_models.append(model)
                if current_arm == "agent":
                    result = runner.run_model_case(
                        case, model, settings, attempt=attempt, on_turn=progress
                    )
                else:
                    result = baseline_runner.run_case(
                        case, model, settings, attempt=attempt, on_turn=progress
                    )
                metrics = dict(
                    result.metrics,
                    arm=current_arm,
                    provider_attempts=model.provider_attempts,
                    input_rejections=model.input_rejections,
                    max_observed_input_estimate=model.max_observed_input_estimate,
                    evidence_mode=report["evidence_mode"],
                    real_api_calls=model.provider_attempts if model_factory is None else None,
                )
                result = replace(result, metrics=metrics)
            except (OSError, KeyboardInterrupt):
                raise  # 报告写失败或中断时保留最后的检查点，不继续花费额度。
            except Exception as error:
                # 不把可能含认证信息的供应商初始化异常原文写入报告。
                result = EvalResult(
                    case.id,
                    False,
                    (CheckResult("setup", False, True, dict(error_type=type(error).__name__)),),
                    dict(
                        arm=current_arm,
                        provider_attempts=model.provider_attempts if model else 0,
                        observations=(report["in_progress"] or {}).get("observations", []),
                    ),
                    attempt=attempt,
                )
            results.append(result.to_dict())
            report["in_progress"] = None
            report["not_run"] = [
                dict(case_id=c.id, attempt=a, arm=arm) for c, a, arm in scheduled[index + 1 :]
            ]
            save()
            if result.metrics.get("budget_rejections", 0) and budget.snapshot()["remaining"] == 0:
                report["status"] = "budget_exhausted"
                break
            if any(o["status"] == "cancelled" for o in result.metrics.get("observations", [])):
                report["status"] = "interrupted"
                break
        else:
            report["status"] = "completed" if all(r["passed"] for r in results) else "failed"
    except KeyboardInterrupt:
        report["status"] = "interrupted"
    except OSError:
        report["status"] = "io_failed"
        raise
    finally:
        report["not_run"] = [
            dict(case_id=c.id, attempt=a, arm=arm)
            for c, a, arm in scheduled[len(results) :]
            if report["in_progress"] is None
            or (c.id, a, arm)
            != (
                report["in_progress"]["case_id"],
                report["in_progress"]["attempt"],
                report["in_progress"]["arm"],
            )
        ]
        save()
    print(
        json.dumps(
            dict(
                status=report["status"],
                passed=report["passed"],
                completed=len(results),
                output=str(options.output),
            ),
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "completed" else 130 if report["status"] == "interrupted" else 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
