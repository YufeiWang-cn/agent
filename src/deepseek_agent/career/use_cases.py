"""求职业务用例：校验业务不变量，并在存储事务内完成状态修改。"""

import re
from uuid import uuid4

from ..timekeeping import now_china
from .domain import (
    CATEGORIES,
    MAX_FACT_HISTORY_PER_FACT,
    CareerDataError,
    Json,
    dependencies_stale,
    normalize_change,
    text,
    validate_fact_source,
)
from .ports import CareerRepository, CareerTransaction
from .rendering import render_resume_draft, render_review


class CareerUseCases:
    def __init__(self, repository: CareerRepository) -> None:
        self.repository = repository

    @property
    def generation(self) -> int:
        return self.repository.generation

    def import_document(self, kind: str, title: str, content: str) -> Json:
        if not isinstance(kind, str) or kind not in {"resume", "job", "resume_draft"}:
            raise CareerDataError("文档类型只能是 resume、job 或 resume_draft。")
        title = text(title, "title", 200)
        content = text(content, "content", 50_000)
        with self.repository.transaction() as db:
            existing = db.find_document(kind, content)
            if existing:
                return dict(existing)
            if db.count("documents") >= 100:
                raise CareerDataError("已保存 100 份文档，请使用新的求职档案。")
            document = dict(
                id=uuid4().hex,
                kind=kind,
                title=title,
                content=content,
                created_at=now_china().isoformat(),
            )
            db.insert_document(document)
        return document

    def document(self, document_id: str) -> Json:
        with self.repository.transaction() as db:
            row = db.document(document_id)
        if row is None:
            raise CareerDataError("来源文档不存在于当前求职档案。")
        return dict(row)

    def build_resume_draft(self, fact_ids: list[str]) -> Json:
        """只组织已确认事实，不让生成措辞反向变成新的事实来源。"""
        if (
            not isinstance(fact_ids, list)
            or not fact_ids
            or len(fact_ids) > 100
            or not all(isinstance(fact_id, str) for fact_id in fact_ids)
        ):
            raise CareerDataError("请选择 1 到 100 条确认事实生成初稿。")
        facts = []
        with self.repository.transaction() as db:
            for fact_id in dict.fromkeys(fact_ids):
                if not isinstance(fact_id, str):
                    raise CareerDataError("事实 ID 必须是字符串。")
                row = db.fact(fact_id)
                if row is None or row["category"] == "feedback":
                    raise CareerDataError(
                        "初稿只能使用当前档案中的教育、项目、技能和求职偏好事实。"
                    )
                facts.append(dict(row))
        return self.import_document("resume_draft", "确认事实整理初稿", render_resume_draft(facts))

    def save_fact(
        self,
        *,
        key: str,
        category: str,
        statement: str,
        source_id: str,
        source_quote: str,
        current_user: str = "",
    ) -> Json:
        key = text(key, "key", 80)
        statement = text(statement, "statement", 800)
        source_quote = text(source_quote, "source_quote", 2000)
        if not isinstance(category, str) or category not in CATEGORIES:
            raise CareerDataError("事实分类无效。")
        if source_id == "current_user":
            source_kind, source = "current_user", current_user
        else:
            document = self.document(source_id)
            source_kind, source = document["kind"], document["content"]
        validate_fact_source(source_kind, source, source_quote)
        with self.repository.transaction() as db:
            previous = db.fact_by_key(key)
            if previous is None and db.count("facts") >= 200:
                raise CareerDataError("确认事实已达 200 条，请先整理或删除不再需要的事实。")
            fact = dict(
                id=previous["id"] if previous else uuid4().hex,
                fact_key=key,
                category=category,
                statement=statement,
                source_id=source_id,
                source_quote=source_quote,
                revision=previous["revision"] + 1 if previous else 1,
                updated_at=now_china().isoformat(),
            )
            db.save_fact(fact, previous, MAX_FACT_HISTORY_PER_FACT)
        return fact

    def forget_fact(self, fact_id: str) -> None:
        with self.repository.transaction() as db:
            if not db.forget_fact(fact_id):
                raise CareerDataError("事实不存在于当前档案。")

    def snapshot(
        self,
        query: str = "",
        limit: int = 20,
        *,
        include_quotes: bool = True,
    ) -> Json:
        """返回事实、文档与评价的轻量快照。

        ``include_quotes=False`` 时不取最长 2000 字的 ``source_quote`` 列，
        供每轮都要调用的运行时上下文使用；recall 等展示场景仍取全列。
        """
        with self.repository.transaction() as db:
            facts = db.list_facts(include_quotes)
            documents = db.document_index()
            reviews = db.review_index()
        revisions = {fact["id"]: fact["revision"] for fact in facts}
        for review in reviews:
            dependencies = review.pop("fact_revisions")
            review["stale"] = dependencies_stale(dependencies, revisions)
        terms = re.findall(r"[a-zA-Z0-9_+#.]+|[\u4e00-\u9fff]{2}", query.lower())
        if terms:
            facts.sort(
                key=lambda fact: sum(
                    term in (fact["fact_key"] + " " + fact["statement"]).lower() for term in terms
                ),
                reverse=True,
            )
        return dict(
            facts=facts[:limit],
            fact_index=[dict(id=f["id"], key=f["fact_key"], revision=f["revision"]) for f in facts],
            documents=documents,
            reviews=reviews,
        )

    def save_review(
        self, *, resume_id: str, job_id: str | None, title: str, markdown: str, changes: list[Json]
    ) -> Json:
        resume = self.document(resume_id)
        if resume["kind"] not in {"resume", "resume_draft"}:
            raise CareerDataError("resume_id 必须指向简历。")
        if job_id and self.document(job_id)["kind"] != "job":
            raise CareerDataError("job_id 必须指向岗位描述。")
        title = text(title, "title", 200)
        markdown = text(markdown, "markdown", 30_000)
        if not isinstance(changes, list) or len(changes) > 30:
            raise CareerDataError("changes 必须是最多 30 条的列表。")
        revisions: Json = {}
        normalized: list[Json] = []
        with self.repository.transaction() as db:
            for change in changes:
                item = normalize_change(change, resume["content"])
                for fact_id in item["evidence_fact_ids"]:
                    fact = db.fact(fact_id)
                    if fact is None:
                        raise CareerDataError("修改引用了不存在或已删除的事实。")
                    revisions[fact_id] = fact["revision"]
                normalized.append(item)
            if db.count("reviews") >= 100:
                raise CareerDataError("修改报告已达 100 份，请使用新的求职档案。")
            review: Json = dict(
                id=uuid4().hex,
                resume_id=resume_id,
                job_id=job_id,
                title=title,
                markdown=markdown,
                changes=normalized,
                fact_revisions=revisions,
                status="draft",
                created_at=now_china().isoformat(),
            )
            db.insert_review(review)
        return self.review(review["id"])

    def review(self, review_id: str) -> Json:
        with self.repository.transaction() as db:
            return self._read_review(db, review_id)

    @staticmethod
    def _read_review(db: CareerTransaction, review_id: str) -> Json:
        review = db.review(review_id)
        if review is None:
            raise CareerDataError("报告不存在于当前求职档案。")
        revisions = {}
        for fact_id in review["fact_revisions"]:
            fact = db.fact(fact_id)
            if fact is not None:
                revisions[fact_id] = fact["revision"]
        review["stale"] = dependencies_stale(review["fact_revisions"], revisions)
        return review

    def decide_review(self, review_id: str, decision: str) -> Json:
        if not isinstance(decision, str) or decision not in {"accepted", "rejected"}:
            raise CareerDataError("decision 只能是 accepted 或 rejected。")
        with self.repository.transaction() as db:
            review = self._read_review(db, review_id)
            if decision == "accepted" and review["stale"]:
                raise CareerDataError("报告引用的事实已更新或删除，请重新生成报告后再接受。")
            db.set_review_status(review_id, decision)
        return self.review(review_id)

    def render_review(self, review_id: str) -> str:
        with self.repository.transaction() as db:
            review = self._read_review(db, review_id)
            if review["stale"]:
                raise CareerDataError("报告依据已变化，请重新生成；旧报告不能继续导出。")
            evidence = []
            for fact_id in review["fact_revisions"]:
                fact = db.fact(fact_id)
                assert fact is not None
                evidence.append(fact)
        return render_review(review, evidence)
