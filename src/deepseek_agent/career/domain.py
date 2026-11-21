"""不依赖模型、数据库或界面的求职数据约束。"""

import re
from typing import Any

Json = dict[str, Any]
CATEGORIES = frozenset({"education", "project", "skill", "preference", "feedback"})
# 每条事实保留的历史修订版本数；反复修订不会让 fact_history 无限膨胀。
MAX_FACT_HISTORY_PER_FACT = 10
PROFILE_ID_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")


class CareerDataError(ValueError):
    """业务数据不符合约束，或保存的数据库不可用。"""


def text(value: Any, field: str, maximum: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CareerDataError(f"{field} 必须是 1 到 {maximum} 字符的非空文本。")
    return value.strip()


def validate_fact_source(source_kind: str, source: str, quote: str) -> None:
    if source_kind not in {"resume", "current_user"}:
        raise CareerDataError("岗位要求不能作为学生经历的事实来源。")
    if quote not in source:
        raise CareerDataError("source_quote 必须逐字来自来源简历或本轮用户原话。")


def normalize_change(change: Json, resume_content: str) -> Json:
    if not isinstance(change, dict):
        raise CareerDataError("每条修改必须是一个对象。")
    original = text(change.get("original"), "original", 2000)
    suggested = text(change.get("suggested"), "suggested", 3000)
    reason = text(change.get("reason"), "reason", 1000)
    if original not in resume_content:
        raise CareerDataError("修改原文必须逐字来自指定简历。")
    evidence = change.get("evidence_fact_ids")
    if not isinstance(evidence, list) or not evidence or len(evidence) > 20:
        raise CareerDataError("每条修改需要 1 到 20 个已确认事实 ID。")
    if not all(isinstance(fact_id, str) for fact_id in evidence):
        raise CareerDataError("事实 ID 必须是字符串。")
    return dict(
        original=original,
        suggested=suggested,
        reason=reason,
        evidence_fact_ids=list(dict.fromkeys(evidence)),
    )


def dependencies_stale(dependencies: Json, current_revisions: Json) -> bool:
    return any(current_revisions.get(key) != revision for key, revision in dependencies.items())


def normalize_findings(findings: object) -> list[Json]:
    """正文中的个人经历判断必须显式引用事实；不替代人工语义审阅。"""
    if not isinstance(findings, list) or len(findings) > 30:
        raise CareerDataError("findings 必须是最多 30 条的列表。")
    normalized = []
    for item in findings:
        if not isinstance(item, dict):
            raise CareerDataError("每条正文判断必须是一个对象。")
        content = text(item.get("text"), "finding.text", 2000)
        ids = item.get("evidence_fact_ids")
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 20
            or not all(isinstance(value, str) and value.strip() for value in ids)
        ):
            raise CareerDataError("每条正文判断需要 1 到 20 个已确认事实 ID。")
        normalized.append(dict(text=content, evidence_fact_ids=list(dict.fromkeys(ids))))
    return normalized


def normalize_advice(advice: object) -> list[str]:
    if not isinstance(advice, list) or len(advice) > 20:
        raise CareerDataError("general_advice 必须是最多 20 条的列表。")
    return [text(item, "general_advice", 1000) for item in advice]


def dependency_issues(dependencies: Json, current: Json, status: str) -> list[Json]:
    issues: list[Json] = []
    if status != "tracked":
        issues.append(dict(reason="unverified_dependencies"))
    for fact_id, expected in dependencies.items():
        actual = current.get(fact_id)
        if actual != expected:
            issues.append(
                dict(
                    reason="fact_deleted" if actual is None else "fact_updated",
                    fact_id=fact_id,
                    expected_revision=expected,
                    current_revision=actual,
                )
            )
    return issues


def require_current_evidence(record: Json, label: str) -> None:
    if not record["stale"]:
        return
    reasons = []
    for issue in record["stale_reasons"]:
        if issue["reason"] == "unverified_dependencies":
            reasons.append("证据依赖不完整，待重新核验")
        elif issue["reason"] == "fact_deleted":
            reasons.append(f"事实 {issue['fact_id']} 已删除")
        else:
            reasons.append(
                f"事实 {issue['fact_id']} 已更新 "
                f"r{issue['expected_revision']}→r{issue['current_revision']}"
            )
    raise CareerDataError(f"{label}不可继续使用：{'；'.join(reasons)}。请重新生成并审阅。")
