"""纯文本渲染：输入业务快照，不读取数据库也不改变状态。"""

from .domain import Json


def render_resume_draft(facts: list[Json]) -> str:
    parts = ["# 简历初稿", "待审阅：以下内容仅整理用户确认的事实。"]
    headings = {
        "education": "教育背景",
        "preference": "求职目标",
        "skill": "技能",
        "project": "项目与实践",
    }
    for category, heading in headings.items():
        selected = [fact for fact in facts if fact["category"] == category]
        if selected:
            parts.append("## " + heading)
            parts.extend("- " + fact["statement"] for fact in selected)
    parts.append("## 草稿依据（非正式简历内容）")
    parts.extend(f"- {fact['id']} revision {fact['revision']}" for fact in facts)
    return "\n\n".join(parts)


def render_review(review: Json, evidence: list[Json]) -> str:
    state = {"draft": "待审阅草稿", "accepted": "已接受", "rejected": "已拒绝"}[review["status"]]
    parts = [
        f"# {review['title']}",
        f"状态：{state}；简历来源：{review['resume_id']}",
        f"岗位来源：{review['job_id'] or '未指定'}",
        review["markdown"],
    ]
    for index, change in enumerate(review["changes"], 1):
        parts.extend(
            [
                f"## 修改 {index}",
                f"原文：{change['original']}",
                f"建议：{change['suggested']}",
                f"理由：{change['reason']}",
                "事实依据：" + "、".join(change["evidence_fact_ids"]),
            ]
        )
    if evidence:
        parts.append("## 确认事实与来源")
        for fact in evidence:
            parts.extend(
                [
                    f"{fact['id']}（revision {fact['revision']}）：{fact['statement']}",
                    f"来源：{fact['source_id']}；原话：{fact['source_quote']}",
                ]
            )
    return "\n\n".join(parts) + "\n"
