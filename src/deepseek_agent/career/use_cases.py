"""求职业务用例：校验业务不变量，并在存储事务内完成状态修改。"""

import re
from uuid import uuid4

from ..timekeeping import now_china
from .domain import (
    CATEGORIES,
    MAX_FACT_HISTORY_PER_FACT,
    CareerDataError,
    Json,
    dependency_issues,
    normalize_findings,
    normalize_advice,
    require_current_evidence,
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
        if not isinstance(kind, str) or kind not in {"resume", "job"}:
            raise CareerDataError("只能导入 resume、job；初稿必须通过 build_resume_draft 生成。")
        title = text(title, "title", 200)
        content = text(content, "content", 50_000)
        with self.repository.transaction() as db:
            return self._insert_document(db, kind, title, content, {})

    @staticmethod
    def _insert_document(
        db: CareerTransaction, kind: str, title: str, content: str, revisions: Json
    ) -> Json:
        existing = db.find_document(kind, content)
        if (
            existing
            and existing["dependency_status"] == "tracked"
            and existing["fact_revisions"] == revisions
        ):
            return dict(existing)
        if db.count("documents") >= 100:
            raise CareerDataError("已保存 100 份文档，请使用新的求职档案。")
        document = dict(
            id=uuid4().hex,
            kind=kind,
            title=title,
            content=content,
            created_at=now_china().isoformat(),
            fact_revisions=revisions,
            dependency_status="tracked",
        )
        db.insert_document(document)
        return document

    @staticmethod
    def _annotate_evidence(record: Json, revisions: Json) -> Json:
        record["stale_reasons"] = dependency_issues(
            record["fact_revisions"], revisions, record["dependency_status"]
        )
        record["stale"] = bool(record["stale_reasons"])
        return record

    @staticmethod
    def _current_revisions(db: CareerTransaction, dependencies: Json) -> Json:
        revisions = {}
        for fact_id in dependencies:
            fact = db.fact(fact_id)
            if fact is not None:
                revisions[fact_id] = fact["revision"]
        return revisions

    @classmethod
    def _read_document(cls, db: CareerTransaction, document_id: str) -> Json:
        row = db.document(document_id)
        if row is None:
            raise CareerDataError("来源文档不存在于当前求职档案。")
        return cls._annotate_evidence(row, cls._current_revisions(db, row["fact_revisions"]))

    def document(self, document_id: str) -> Json:
        with self.repository.transaction() as db:
            return self._read_document(db, document_id)

    def build_resume_draft(self, fact_ids: list[str]) -> Json:
        """事实读取、依赖采集与初稿保存共用事务；不信任文本中的版本标记。"""
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
                row = db.fact(fact_id)
                if row is None or row["category"] == "feedback":
                    raise CareerDataError(
                        "初稿只能使用当前档案中的教育、项目、技能和求职偏好事实。"
                    )
                facts.append(dict(row))
            content = text(render_resume_draft(facts), "content", 50_000)
            return self._insert_document(
                db,
                "resume_draft",
                "确认事实整理初稿",
                content,
                {fact["id"]: fact["revision"] for fact in facts},
            )

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
        for record in [*documents, *reviews]:
            self._annotate_evidence(record, revisions)
            record.pop("fact_revisions")
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
        self,
        *,
        resume_id: str,
        job_id: str | None,
        title: str,
        changes: list[Json],
        markdown: str = "",
        findings: list[Json] | None = None,
        general_advice: list[str] | None = None,
    ) -> Json:
        title = text(title, "title", 200)
        if not isinstance(changes, list) or len(changes) > 30:
            raise CareerDataError("changes 必须是最多 30 条的列表。")
        # 兼容旧调用的内容存档，但绝不把自由正文升级为已追踪依赖。
        legacy = findings is None
        if legacy:
            markdown = text(markdown, "markdown", 30_000)
            if general_advice is not None:
                raise CareerDataError("结构化报告必须显式提供 findings。")
            body: list[Json] = []
            advice: list[str] = []
        else:
            if markdown != "":
                raise CareerDataError(
                    "结构化报告不接受自由 markdown，请使用 findings 和 general_advice。"
                )
            body = normalize_findings(findings)
            advice = normalize_advice(general_advice if general_advice is not None else [])
            if not body and not advice and not changes:
                raise CareerDataError("报告至少需要一条正文判断、一般建议或修改。")
        normalized: list[Json] = []
        with self.repository.transaction() as db:
            resume = self._read_document(db, resume_id)
            if resume["kind"] not in {"resume", "resume_draft"}:
                raise CareerDataError("resume_id 必须指向简历。")
            require_current_evidence(resume, "来源简历初稿")
            if job_id and self._read_document(db, job_id)["kind"] != "job":
                raise CareerDataError("job_id 必须指向岗位描述。")
            revisions = dict(resume["fact_revisions"])
            for change in changes:
                normalized.append(normalize_change(change, resume["content"]))
            for item in [*body, *normalized]:
                for fact_id in item["evidence_fact_ids"]:
                    fact = db.fact(fact_id)
                    if fact is None:
                        raise CareerDataError("报告引用了不存在或已删除的事实。")
                    revisions[fact_id] = fact["revision"]
            if db.count("reviews") >= 100:
                raise CareerDataError("修改报告已达 100 份，请使用新的求职档案。")
            review: Json = dict(
                id=uuid4().hex,
                resume_id=resume_id,
                job_id=job_id,
                title=title,
                markdown=markdown,
                findings=body,
                general_advice=advice,
                changes=normalized,
                fact_revisions=revisions,
                dependency_status="needs_review" if legacy else "tracked",
                status="draft",
                created_at=now_china().isoformat(),
            )
            db.insert_review(review)
            return self._annotate_evidence(review, revisions)

    def review(self, review_id: str) -> Json:
        with self.repository.transaction() as db:
            return self._read_review(db, review_id)

    @classmethod
    def _read_review(cls, db: CareerTransaction, review_id: str) -> Json:
        review = db.review(review_id)
        if review is None:
            raise CareerDataError("报告不存在于当前求职档案。")
        return cls._annotate_evidence(review, cls._current_revisions(db, review["fact_revisions"]))

    def decide_review(self, review_id: str, decision: str) -> Json:
        if not isinstance(decision, str) or decision not in {"accepted", "rejected"}:
            raise CareerDataError("decision 只能是 accepted 或 rejected。")
        with self.repository.transaction() as db:
            review = self._read_review(db, review_id)
            if decision == "accepted":
                require_current_evidence(review, "报告")
            db.set_review_status(review_id, decision)
            review["status"] = decision
            return review

    def render_review(self, review_id: str) -> str:
        with self.repository.transaction() as db:
            review = self._read_review(db, review_id)
            require_current_evidence(review, "报告")
            evidence = []
            for fact_id in review["fact_revisions"]:
                fact = db.fact(fact_id)
                assert fact is not None
                evidence.append(fact)
        return render_review(review, evidence)
