"""支持 v1/v2→v3 事务迁移的 SQLite 适配器；每个业务事务共用一个连接。"""

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from ..career.domain import CareerDataError, Json
from ..career.ports import CareerTransaction, Collection, ExportTarget


class SqliteCareerRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        # 进程内数据版本号：每次写操作自增，供运行时上下文等派生缓存失效。
        self.generation = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise CareerDataError("求职数据库版本不受支持，请保留文件并升级程序。")
            if version == 3:
                return
            if version == 2:
                self._backup(2)
                self._migrate_v2(db)
                return
            existing = (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'"
                ).fetchone()
                is not None
            )
            if existing:
                self._backup_v1()
            # 不使用 executescript：它会隐式提交，破坏迁移失败时的整体回滚。
            for statement in (
                "CREATE TABLE IF NOT EXISTS documents ("
                "id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, "
                "content TEXT NOT NULL, created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS facts ("
                "id TEXT PRIMARY KEY, fact_key TEXT NOT NULL UNIQUE, "
                "category TEXT NOT NULL, statement TEXT NOT NULL, source_id TEXT NOT NULL, "
                "source_quote TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS fact_history ("
                "fact_id TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL, "
                "PRIMARY KEY(fact_id, revision))",
                "CREATE TABLE IF NOT EXISTS reviews ("
                "id TEXT PRIMARY KEY, resume_id TEXT NOT NULL, job_id TEXT, "
                "title TEXT NOT NULL, markdown TEXT NOT NULL, changes TEXT NOT NULL, "
                "fact_revisions TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)",
            ):
                db.execute(statement)
            self._migrate_v1(db)
            self._migrate_v2(db)

    def _backup_v1(self) -> None:
        self._backup(1)

    def _backup(self, version: int) -> None:
        """持有主连接写锁时通过独立只读连接备份，包含 WAL 中已提交数据。"""
        backup = self.path.with_name(self.path.name + f".v{version}-{uuid4().hex}.bak")
        try:
            with closing(
                sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
            ) as source:
                with closing(sqlite3.connect(backup)) as destination:
                    source.backup(destination)
        except (sqlite3.Error, OSError) as error:
            # 原库尚未修改；不把可能不完整的备份误标为可恢复文件。
            backup.unlink(missing_ok=True)
            raise CareerDataError("升级前备份失败，数据库未升级，请检查磁盘空间和权限。") from error

    @staticmethod
    def _migrate_v1(db: sqlite3.Connection) -> None:
        db.execute("ALTER TABLE documents ADD COLUMN fact_revisions TEXT NOT NULL DEFAULT '{}'")
        db.execute(
            "ALTER TABLE documents ADD COLUMN dependency_status TEXT NOT NULL DEFAULT 'needs_review'"
        )
        db.execute(
            "UPDATE documents SET dependency_status='tracked' WHERE kind IN ('resume','job')"
        )
        db.execute("ALTER TABLE reviews ADD COLUMN findings TEXT NOT NULL DEFAULT '[]'")
        db.execute("ALTER TABLE reviews ADD COLUMN general_advice TEXT NOT NULL DEFAULT '[]'")
        db.execute(
            "ALTER TABLE reviews ADD COLUMN dependency_status TEXT NOT NULL DEFAULT 'needs_review'"
        )
        db.execute("PRAGMA user_version = 2")

    @staticmethod
    def _migrate_v2(db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE exports (id TEXT PRIMARY KEY, review_id TEXT NOT NULL, "
            "path TEXT NOT NULL, workspace_id TEXT NOT NULL, content_sha256 TEXT NOT NULL, "
            "byte_count INTEGER NOT NULL, review_status TEXT NOT NULL, fact_revisions TEXT NOT NULL, "
            "created_at TEXT NOT NULL, finished_at TEXT, error_type TEXT, "
            "result TEXT NOT NULL CHECK(result IN ('pending','succeeded','unconfirmed')))"
        )
        db.execute("CREATE INDEX exports_review_created ON exports(review_id, created_at)")
        db.execute("PRAGMA user_version = 3")

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
        return self._decode_document(
            self._one(
                "SELECT * FROM documents WHERE kind=? AND content=? ORDER BY created_at DESC LIMIT 1",
                (kind, content),
            )
        )

    def document(self, document_id: str) -> Json | None:
        return self._decode_document(
            self._one("SELECT * FROM documents WHERE id=?", (document_id,))
        )

    @staticmethod
    def _decode_document(row: Json | None) -> Json | None:
        if row is not None:
            row["fact_revisions"] = json.loads(row["fact_revisions"])
        return row

    def insert_document(self, document: Json) -> None:
        self._db.execute(
            "INSERT INTO documents (id,kind,title,content,created_at,fact_revisions,dependency_status) "
            "VALUES (:id,:kind,:title,:content,:created_at,:fact_revisions,:dependency_status)",
            dict(document, fact_revisions=json.dumps(document["fact_revisions"])),
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
        rows = [
            dict(row)
            for row in self._db.execute(
                "SELECT id,kind,title,created_at,fact_revisions,dependency_status FROM documents ORDER BY created_at DESC"
            )
        ]

        for row in rows:
            row["fact_revisions"] = json.loads(row["fact_revisions"])
        return rows

    def review_index(self) -> list[Json]:
        rows = [
            dict(row)
            for row in self._db.execute(
                "SELECT id,title,status,resume_id,job_id,fact_revisions,dependency_status FROM reviews ORDER BY created_at DESC"
            )
        ]
        for row in rows:
            row["fact_revisions"] = json.loads(row["fact_revisions"])
        return rows

    def review(self, review_id: str) -> Json | None:
        row = self._one("SELECT * FROM reviews WHERE id=?", (review_id,))
        if row is not None:
            row["changes"] = json.loads(row["changes"])
            row["findings"] = json.loads(row["findings"])
            row["general_advice"] = json.loads(row["general_advice"])
            row["fact_revisions"] = json.loads(row["fact_revisions"])
        return row

    def insert_review(self, review: Json) -> None:
        row = dict(
            review,
            changes=json.dumps(review["changes"], ensure_ascii=False),
            findings=json.dumps(review["findings"], ensure_ascii=False),
            general_advice=json.dumps(review["general_advice"], ensure_ascii=False),
            fact_revisions=json.dumps(review["fact_revisions"]),
        )
        self._db.execute(
            "INSERT INTO reviews (id,resume_id,job_id,title,markdown,changes,fact_revisions,status,"
            "created_at,findings,general_advice,dependency_status) VALUES "
            "(:id,:resume_id,:job_id,:title,:markdown,:changes,:fact_revisions,:status,:created_at,"
            ":findings,:general_advice,:dependency_status)",
            row,
        )

    def set_review_status(self, review_id: str, status: str) -> None:
        self._db.execute("UPDATE reviews SET status=? WHERE id=?", (status, review_id))

    def insert_export(self, record: Json) -> None:
        self._db.execute(
            "INSERT INTO exports (id,review_id,path,workspace_id,content_sha256,byte_count,"
            "review_status,fact_revisions,created_at,finished_at,error_type,result) VALUES "
            "(:id,:review_id,:path,:workspace_id,:content_sha256,:byte_count,"
            ":review_status,:fact_revisions,:created_at,:finished_at,:error_type,:result)",
            dict(record, fact_revisions=json.dumps(record["fact_revisions"])),
        )

    @staticmethod
    def _decode_export(row: Json | None) -> Json | None:
        if row is not None:
            row["fact_revisions"] = json.loads(row["fact_revisions"])
        return row

    def export_record(self, export_id: str) -> Json | None:
        return self._decode_export(self._one("SELECT * FROM exports WHERE id=?", (export_id,)))

    def list_exports(self, review_id: str | None) -> list[Json]:
        where = " WHERE review_id=?" if review_id is not None else ""
        rows = self._db.execute(
            "SELECT * FROM exports" + where + " ORDER BY created_at DESC, id DESC LIMIT 50",
            (review_id,) if review_id is not None else (),
        )
        return [dict(row, fact_revisions=json.loads(row["fact_revisions"])) for row in rows]

    def latest_export(self, target: ExportTarget) -> Json | None:
        # 定向查整个历史，不受展示列表最近 50 条的限制；按插入顺序选择最近意图。
        # Windows 普通路径大小写别名不能绕过已有记录；其他平台保持大小写敏感。
        collation = "NOCASE" if os.name == "nt" else "BINARY"
        return self._decode_export(
            self._one(
                "SELECT * FROM exports WHERE workspace_id=? AND path=? COLLATE "
                + collation
                + " ORDER BY rowid DESC LIMIT 1",
                (target.workspace_id, target.path),
            )
        )

    def finish_export(
        self, export_id: str, result: str, finished_at: str, error_type: str | None
    ) -> None:
        self._db.execute(
            "UPDATE exports SET result=?,finished_at=?,error_type=? WHERE id=?",
            (result, finished_at, error_type, export_id),
        )
