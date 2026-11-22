"""仅离线的求职评测入口；不读取 .env，也不提供真实模型执行开关。"""

import argparse
import json
from collections import Counter
from pathlib import Path

from ..config import PROJECT_ROOT
from .career_cases import load_career_cases
from .career_runner import CareerEvalRunner
from .models import EvalCaseError


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="求职离线脚本评测（不会调用真实模型或网络）。")
    parser.add_argument("--cases", type=Path, default=PROJECT_ROOT / "evals/career/cases")
    parser.add_argument(
        "--output", type=Path, default=PROJECT_ROOT / "data/evals/career-offline.json"
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--compare-controls",
        action="store_true",
        help="对正常闭环案例附加缺失业务快照的评分器负对照。",
    )
    options = parser.parse_args(argv)
    if not 1 <= options.repeat <= 10:
        parser.error("repeat 必须在 1–10 之间。")
    try:
        cases = load_career_cases(options.cases)
    except (EvalCaseError, TypeError) as error:
        parser.error(str(error))
    runner = CareerEvalRunner()
    results = [
        runner.run_case(case, attempt=attempt)
        for case in cases
        for attempt in range(1, options.repeat + 1)
    ]
    controls = [
        runner.run_case(case, omit_state_control=True)
        for case in cases
        if options.compare_controls and case.id in {"01_resume_jd", "02_zero_resume"}
    ]
    failures = Counter(
        check.name.split(".")[-1]
        for result in results
        for check in result.checks
        if not check.passed
    )
    passed = sum(result.passed for result in results)
    payload = dict(
        evidence_mode="offline-script",
        real_api_calls=0,
        scope="验证工具链与评分器，不衡量模型推理、措辞忠实度或提示注入识别能力。",
        cases=len(cases),
        attempts=len(results),
        passed=passed,
        success_rate=passed / len(results),
        failure_categories=dict(failures),
        results=[result.to_dict() for result in results],
        negative_controls=[result.to_dict() for result in controls],
        controls_detected=sum(not result.passed for result in controls),
        human_review=dict(status="not_run", rubric="evals/career/README.md"),
    )
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"离线脚本：{passed}/{len(results)} 通过；负对照检出 {payload['controls_detected']}/{len(controls)}；真实 API 调用 0。"
    )
    print(f"报告：{options.output}")
    return 0 if passed == len(results) and all(not result.passed for result in controls) else 1


if __name__ == "__main__":
    raise SystemExit(run())
