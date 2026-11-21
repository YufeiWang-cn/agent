"""兼容 v1 档案的 SQLite 适配器；每个业务事务共用一个连接。"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..career.domain import CareerDataError, Json
from ..career.ports import CareerTransaction, Collection


class SqliteCareerRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        # 进程内数据版本号：每次写操作自增，供运行时上下文等派生缓存失效。
        self.generation = 0
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

    @contextmanager
    def transaction(self) -> Iterator[CareerTransaction]:
        with self.connection() as db:
            before = db.total_changes
            yield SqliteCareerTransaction(db)
            changed = db.total_changes != before
        # 只有成功提交之后，才让派生上下文失效。
        if changed:
            self.generation += 1


class SqliteCareerTransaction:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def _one(self, sql: str, values: tuple[object, ...]) -> Json | None:
        row = self._db.execute(sql, values).fetchone()
        return dict(row) if row is not None else None

    def count(self, collection: Collection) -> int:
        if collection not in {"documents", "facts", "reviews"}:
            raise ValueError("Unknown collection")
        return int(self._db.execute(f"SELECT COUNT(*) FROM {collection}").fetchone()[0])

    def find_document(self, kind: str, content: str) -> Json | None:
        return self._one("SELECT * FROM documents WHERE kind=? AND content=?", (kind, content))

    def document(self, document_id: str) -> Json | None:
        return self._one("SELECT * FROM documents WHERE id=?", (document_id,))

    def insert_document(self, document: Json) -> None:
        self._db.execute(
            "INSERT INTO documents VALUES (:id,:kind,:title,:content,:created_at)", document
        )

    def fact(self, fact_id: str) -> Json | None:
        return self._one("SELECT * FROM facts WHERE id=?", (fact_id,))

    def fact_by_key(self, key: str) -> Json | None:
        return self._one("SELECT * FROM facts WHERE fact_key=?", (key,))

    def save_fact(self, fact: Json, previous: Json | None, history_limit: int) -> None:
        if previous:
            self._db.execute(
                "INSERT INTO fact_history VALUES (?,?,?)",
                (previous["id"], previous["revision"], json.dumps(previous, ensure_ascii=False)),
            )
            self._db.execute(
                "DELETE FROM fact_history WHERE fact_id=? AND revision<=?",
                (previous["id"], previous["revision"] - history_limit),
            )
        self._db.execute(
            "INSERT OR REPLACE INTO facts VALUES "
            "(:id,:fact_key,:category,:statement,:source_id,:source_quote,:revision,:updated_at)",
            fact,
        )

    def forget_fact(self, fact_id: str) -> bool:
        deleted = self._db.execute("DELETE FROM facts WHERE id=?", (fact_id,)).rowcount > 0
        self._db.execute("DELETE FROM fact_history WHERE fact_id=?", (fact_id,))
        return deleted

    def list_facts(self, include_quotes: bool) -> list[Json]:
        columns = (
            "*"
            if include_quotes
            else "id,fact_key,category,statement,source_id,revision,updated_at"
        )
        return [
            dict(row)
            for row in self._db.execute(f"SELECT {columns} FROM facts ORDER BY updated_at DESC")
        ]

    def document_index(self) -> list[Json]:
        return [
            dict(row)
            for row in self._db.execute(
                "SELECT id,kind,title,created_at FROM documents ORDER BY created_at DESC"
            )
        ]

    def review_index(self) -> list[Json]:
        rows = [
            dict(row)
            for row in self._db.execute(
                "SELECT id,title,status,resume_id,job_id,fact_revisions FROM reviews ORDER BY created_at DESC"
            )
        ]
        for row in rows:
            row["fact_revisions"] = json.loads(row["fact_revisions"])
        return rows

    def review(self, review_id: str) -> Json | None:
        row = self._one("SELECT * FROM reviews WHERE id=?", (review_id,))
        if row is not None:
            row["changes"] = json.loads(row["changes"])
            row["fact_revisions"] = json.loads(row["fact_revisions"])
        return row

    def insert_review(self, review: Json) -> None:
        row = dict(
            review,
            changes=json.dumps(review["changes"], ensure_ascii=False),
            fact_revisions=json.dumps(review["fact_revisions"]),
        )
        self._db.execute(
            "INSERT INTO reviews VALUES "
            "(:id,:resume_id,:job_id,:title,:markdown,:changes,:fact_revisions,:status,:created_at)",
            row,
        )

    def set_review_status(self, review_id: str, status: str) -> None:
        self._db.execute("UPDATE reviews SET status=? WHERE id=?", (status, review_id))
