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
