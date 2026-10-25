"""以事务保存来源、确认事实、纠正历史及岗位修改草稿。"""

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from ..timekeeping import now_china

Json = dict[str, Any]
CATEGORIES = frozenset({"education", "project", "skill", "preference", "feedback"})
PROFILE_ID_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")


class CareerDataError(ValueError):
    """业务数据不符合约束，或保存的数据库不可用。"""


def text(value: Any, field: str, maximum: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CareerDataError(f"{field} 必须是 1 到 {maximum} 字符的非空文本。")
    return value.strip()


class CareerStore:
    """一个档案一个数据库；所有事实写入由上层用户确认流程授权。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise CareerDataError("求职数据库版本不受支持，请保留文件并升级程序。")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                    content TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS facts (
                    id TEXT PRIMARY KEY, fact_key TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL, statement TEXT NOT NULL,
                    source_id TEXT NOT NULL, source_quote TEXT NOT NULL,
                    revision INTEGER NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fact_history (
                    fact_id TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(fact_id, revision));
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY, resume_id TEXT NOT NULL, job_id TEXT,
                    title TEXT NOT NULL, markdown TEXT NOT NULL, changes TEXT NOT NULL,
                    fact_revisions TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                PRAGMA user_version = 1;
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5)
            with db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                yield db
        except sqlite3.Error as error:
            raise CareerDataError(
                "求职数据读取或保存失败；原数据库已保留，请检查文件和权限。"
            ) from error
        finally:
            if db is not None:
                db.close()

    def import_document(self, kind: str, title: str, content: str) -> Json:
        if not isinstance(kind, str) or kind not in {"resume", "job", "resume_draft"}:
            raise CareerDataError("文档类型只能是 resume、job 或 resume_draft。")
        title = text(title, "title", 200)
        content = text(content, "content", 50_000)
        with self.connection() as db:
            existing = db.execute(
                "SELECT * FROM documents WHERE kind=? AND content=?", (kind, content)
            ).fetchone()
            if existing:
                return dict(existing)
            if db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] >= 100:
                raise CareerDataError("已保存 100 份文档，请使用新的求职档案。")
            document = dict(
                id=uuid4().hex,
                kind=kind,
                title=title,
                content=content,
                created_at=now_china().isoformat(),
            )
            db.execute(
                "INSERT INTO documents VALUES (:id,:kind,:title,:content,:created_at)", document
            )
        return document

    def document(self, document_id: str) -> Json:
        with self.connection() as db:
            row = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
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
        with self.connection() as db:
            for fact_id in dict.fromkeys(fact_ids):
                if not isinstance(fact_id, str):
                    raise CareerDataError("事实 ID 必须是字符串。")
                row = db.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
                if row is None or row["category"] == "feedback":
                    raise CareerDataError(
                        "初稿只能使用当前档案中的教育、项目、技能和求职偏好事实。"
                    )
                facts.append(dict(row))
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
        return self.import_document("resume_draft", "确认事实整理初稿", "\n\n".join(parts))

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
            source = current_user
        else:
            document = self.document(source_id)
            if document["kind"] != "resume":
                raise CareerDataError("岗位要求不能作为学生经历的事实来源。")
            source = document["content"]
        if source_quote not in source:
            raise CareerDataError("source_quote 必须逐字来自来源简历或本轮用户原话。")
        with self.connection() as db:
            previous = db.execute("SELECT * FROM facts WHERE fact_key=?", (key,)).fetchone()
            if previous is None and db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] >= 200:
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
            if previous:
                db.execute(
                    "INSERT INTO fact_history VALUES (?,?,?)",
                    (
                        previous["id"],
                        previous["revision"],
                        json.dumps(dict(previous), ensure_ascii=False),
                    ),
                )
            db.execute(
                "INSERT OR REPLACE INTO facts VALUES "
                "(:id,:fact_key,:category,:statement,:source_id,:source_quote,:revision,:updated_at)",
                fact,
            )
        return fact

    def forget_fact(self, fact_id: str) -> None:
        with self.connection() as db:
            if db.execute("DELETE FROM facts WHERE id=?", (fact_id,)).rowcount == 0:
                raise CareerDataError("事实不存在于当前档案。")
            db.execute("DELETE FROM fact_history WHERE fact_id=?", (fact_id,))

    def snapshot(self, query: str = "", limit: int = 20) -> Json:
        with self.connection() as db:
            facts = [
                dict(row) for row in db.execute("SELECT * FROM facts ORDER BY updated_at DESC")
            ]
            documents = [
                dict(row)
                for row in db.execute(
                    "SELECT id,kind,title,created_at FROM documents ORDER BY created_at DESC"
                )
            ]
            reviews = [
                dict(row)
                for row in db.execute(
                    "SELECT id,title,status,resume_id,job_id,fact_revisions FROM reviews ORDER BY created_at DESC"
                )
            ]
        revisions = {fact["id"]: fact["revision"] for fact in facts}
        for review in reviews:
            dependencies = json.loads(review.pop("fact_revisions"))
            review["stale"] = any(
                revisions.get(key) != revision for key, revision in dependencies.items()
            )
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
        with self.connection() as db:
            for change in changes:
                if not isinstance(change, dict):
                    raise CareerDataError("每条修改必须是一个对象。")
                original = text(change.get("original"), "original", 2000)
                suggested = text(change.get("suggested"), "suggested", 3000)
                reason = text(change.get("reason"), "reason", 1000)
                if original not in resume["content"]:
                    raise CareerDataError("修改原文必须逐字来自指定简历。")
                evidence = change.get("evidence_fact_ids")
                if not isinstance(evidence, list) or not evidence or len(evidence) > 20:
                    raise CareerDataError("每条修改需要 1 到 20 个已确认事实 ID。")
                for fact_id in evidence:
                    if not isinstance(fact_id, str):
                        raise CareerDataError("事实 ID 必须是字符串。")
                    fact = db.execute(
                        "SELECT revision FROM facts WHERE id=?", (fact_id,)
                    ).fetchone()
                    if fact is None:
                        raise CareerDataError("修改引用了不存在或已删除的事实。")
                    revisions[fact_id] = fact["revision"]
                normalized.append(
                    dict(
                        original=original,
                        suggested=suggested,
                        reason=reason,
                        evidence_fact_ids=list(dict.fromkeys(evidence)),
                    )
                )
            if db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] >= 100:
                raise CareerDataError("修改报告已达 100 份，请使用新的求职档案。")
            review: Json = dict(
                id=uuid4().hex,
                resume_id=resume_id,
                job_id=job_id,
                title=title,
                markdown=markdown,
                changes=json.dumps(normalized, ensure_ascii=False),
                fact_revisions=json.dumps(revisions),
                status="draft",
                created_at=now_china().isoformat(),
            )
            db.execute(
                "INSERT INTO reviews VALUES "
                "(:id,:resume_id,:job_id,:title,:markdown,:changes,:fact_revisions,:status,:created_at)",
                review,
            )
        return self.review(review["id"])

    def review(self, review_id: str) -> Json:
        with self.connection() as db:
            return self._read_review(db, review_id)

    @staticmethod
    def _read_review(db: sqlite3.Connection, review_id: str) -> Json:
        row = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise CareerDataError("报告不存在于当前求职档案。")
        result = dict(row)
        result["changes"] = json.loads(result["changes"])
        result["fact_revisions"] = json.loads(result["fact_revisions"])
        result["stale"] = any(
            (fact := db.execute("SELECT revision FROM facts WHERE id=?", (fact_id,)).fetchone())
            is None
            or fact["revision"] != revision
            for fact_id, revision in result["fact_revisions"].items()
        )
        return result

    def decide_review(self, review_id: str, decision: str) -> Json:
        if not isinstance(decision, str) or decision not in {"accepted", "rejected"}:
            raise CareerDataError("decision 只能是 accepted 或 rejected。")
        with self.connection() as db:
            review = self._read_review(db, review_id)
            if decision == "accepted" and review["stale"]:
                raise CareerDataError("报告引用的事实已更新或删除，请重新生成报告后再接受。")
            db.execute("UPDATE reviews SET status=? WHERE id=?", (decision, review_id))
        return self.review(review_id)

    def render_review(self, review_id: str) -> str:
        with self.connection() as db:
            review = self._read_review(db, review_id)
            evidence = (
                [
                    dict(db.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone())
                    for fact_id in review["fact_revisions"]
                ]
                if not review["stale"]
                else []
            )
        if review["stale"]:
            raise CareerDataError("报告依据已变化，请重新生成；旧报告不能继续导出。")
        state = {"draft": "待审阅草稿", "accepted": "已接受", "rejected": "已拒绝"}[
            review["status"]
        ]
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
