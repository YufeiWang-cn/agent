"""提供运行本地端到端 Agent 评测的命令行入口。"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import cast

from ..agent import Agent
from ..config import PROJECT_ROOT, Settings
from ..journal import RunJournal
from ..memory import JsonProjectStore, JsonSessionStore
from ..timekeeping import now_china
from .models import EvalCase, EvalCaseError, EvalResult, load_cases
from .runner import AgentFactory, EvalRunner, EvalToolConfirmer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="在临时工作区运行 DeepSeek Agent 端到端评测。"
    )
    parser.add_argument(
        "cases",
        nargs="?",
        type=Path,
        default=PROJECT_ROOT / "evals" / "cases",
        help="单个 JSON 案例或案例目录。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="JSON 报告路径；默认写入 data/evals/。",
    )
    parser.add_argument(
        "--keep-failed-workspaces",
        action="store_true",
        help="把失败案例的最终工作区保存到报告旁边。",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="每个案例独立重复运行次数（默认 1）。",
    )
    parser.add_argument(
        "--input-price-per-million",
        type=float,
        help="每百万输入 Token 的价格；需与输出价格同时提供。",
    )
    parser.add_argument(
        "--output-price-per-million",
        type=float,
        help="每百万输出 Token 的价格；需与输入价格同时提供。",
    )
    return parser


def _agent_factory(settings: Settings) -> AgentFactory:
    def create(workspace: Path, state: Path, case: EvalCase) -> Agent:
        effective_settings = replace(settings, workspace_root=workspace)
        return Agent(
            effective_settings,
            session_store=JsonSessionStore(state / "sessions"),
            project_store=JsonProjectStore(state / "projects.json"),
            confirmer=EvalToolConfirmer(case.approve_tools),
            run_journal=RunJournal(state / "runs"),
        )

    return create


def _default_output_path() -> Path:
    timestamp = now_china().strftime("%Y%m%dT%H%M%S%z")
    return PROJECT_ROOT / "data" / "evals" / f"eval-{timestamp}.json"


def _report_payload(
    settings: Settings,
    results: tuple[EvalResult, ...],
    *,
    input_price_per_million: float | None = None,
    output_price_per_million: float | None = None,
) -> dict[str, object]:
    passed = sum(result.passed for result in results)
    case_aggregates = _case_aggregates(
        results,
        input_price_per_million,
        output_price_per_million,
    )
    case_passed = sum(item["success_rate"] == 1.0 for item in case_aggregates)
    safety_passed = sum(
        any(check.name == "safety" and check.passed for check in result.checks)
        for result in results
    )
    total_input_tokens = sum(
        int(cast(int, item["input_tokens"])) for item in case_aggregates
    )
    total_output_tokens = sum(
        int(cast(int, item["output_tokens"])) for item in case_aggregates
    )
    total_estimated_cost = (
        None
        if input_price_per_million is None or output_price_per_million is None
        else round(
            sum(cast(float, item["estimated_cost"]) for item in case_aggregates),
            8,
        )
    )
    return {
        "generated_at": now_china().isoformat(),
        "model": settings.model,
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "pass_rate": round(passed / len(results), 4) if results else 0.0,
            "total_cases": len(case_aggregates),
            "fully_passed_cases": case_passed,
            "total_attempts": len(results),
            "safety_passed": safety_passed,
            "safety_rate": (
                round(safety_passed / len(results), 4) if results else 0.0
            ),
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "total_tokens": total_input_tokens + total_output_tokens,
            "estimated_cost": total_estimated_cost,
        },
        "pricing": (
            None
            if input_price_per_million is None or output_price_per_million is None
            else {
                "input_price_per_million": input_price_per_million,
                "output_price_per_million": output_price_per_million,
                "note": "成本按报告 Token 计算；estimated/mixed 不是账单精确值。",
            }
        ),
        "case_aggregates": case_aggregates,
        "results": [result.to_dict() for result in results],
    }


def _metric_number(result: EvalResult, key: str) -> float:
    value = result.metrics.get(key, 0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _percentile_95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _case_aggregates(
    results: tuple[EvalResult, ...],
    input_price_per_million: float | None,
    output_price_per_million: float | None,
) -> list[dict[str, object]]:
    grouped: dict[str, list[EvalResult]] = defaultdict(list)
    for result in results:
        grouped[result.case_id].append(result)

    aggregates: list[dict[str, object]] = []
    for case_id, attempts in grouped.items():
        durations = [_metric_number(item, "duration_seconds") for item in attempts]
        input_tokens = int(sum(_metric_number(item, "input_tokens") for item in attempts))
        output_tokens = int(
            sum(_metric_number(item, "output_tokens") for item in attempts)
        )
        safety_passed = sum(
            any(
                check.name == "safety" and check.passed
                for check in item.checks
            )
            for item in attempts
        )
        passed_attempts = sum(item.passed for item in attempts)
        token_sources = {
            str(item.metrics.get("token_source", "unavailable"))
            for item in attempts
        }
        estimated_cost = None
        if (
            input_price_per_million is not None
            and output_price_per_million is not None
        ):
            estimated_cost = round(
                input_tokens / 1_000_000 * input_price_per_million
                + output_tokens / 1_000_000 * output_price_per_million,
                8,
            )
        aggregates.append(
            {
                "case_id": case_id,
                "attempts": len(attempts),
                "passed": passed_attempts,
                "success_rate": round(passed_attempts / len(attempts), 4),
                "safety_rate": round(safety_passed / len(attempts), 4),
                "median_duration_seconds": round(statistics.median(durations), 3),
                "p95_duration_seconds": round(_percentile_95(durations), 3),
                "median_tool_calls": statistics.median(
                    [_metric_number(item, "tool_calls") for item in attempts]
                ),
                "median_model_requests": statistics.median(
                    [_metric_number(item, "model_requests") for item in attempts]
                ),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "token_sources": sorted(token_sources),
                "estimated_cost": estimated_cost,
            }
        )
    return aggregates


def run(arguments: list[str] | None = None) -> int:
    """运行评测并返回适合自动化脚本使用的退出码。"""
    options = build_parser().parse_args(arguments)
    prices = (
        options.input_price_per_million,
        options.output_price_per_million,
    )
    if options.repeat <= 0:
        print("评测配置错误：--repeat 必须是正整数。", file=sys.stderr)
        return 2
    if any(
        price is not None and (not math.isfinite(price) or price < 0)
        for price in prices
    ):
        print("评测配置错误：Token 价格必须是有限非负数。", file=sys.stderr)
        return 2
    if (prices[0] is None) != (prices[1] is None):
        print("评测配置错误：输入和输出 Token 价格必须同时提供。", file=sys.stderr)
        return 2
    try:
        cases = load_cases(options.cases.resolve())
        settings = Settings.from_env()
    except (EvalCaseError, RuntimeError) as error:
        print(f"评测配置错误：{error}", file=sys.stderr)
        return 2

    output = (options.output or _default_output_path()).resolve()
    failed_directory = (
        output.parent / f"{output.stem}-workspaces"
        if options.keep_failed_workspaces
        else None
    )
    runner = EvalRunner(
        _agent_factory(settings),
        failed_workspace_directory=failed_directory,
    )
    results = runner.run_cases(cases, repeat=options.repeat)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            _report_payload(
                settings,
                results,
                input_price_per_million=options.input_price_per_million,
                output_price_per_million=options.output_price_per_million,
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        print(f"[{mark}] {result.case_id} (attempt {result.attempt})")
        if not result.passed:
            for check in result.checks:
                if not check.passed:
                    print(f"  - {check.name}: {check.details}")
    passed = sum(result.passed for result in results)
    print(f"结果：{passed}/{len(results)} 通过")
    if options.repeat > 1:
        for aggregate in _case_aggregates(results, prices[0], prices[1]):
            success_rate = cast(float, aggregate["success_rate"])
            safety_rate = cast(float, aggregate["safety_rate"])
            print(
                f"  {aggregate['case_id']}: "
                f"成功率 {success_rate:.1%}, "
                f"安全率 {safety_rate:.1%}"
            )
    print(f"报告：{output}")
    return 0 if passed == len(results) else 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
