"""使用独立 JSON 文件持久化会话，并支持短 ID 查找。"""

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..timekeeping import timestamp_sort_key
from .session import Session


logger = logging.getLogger(__name__)


class SessionStoreError(RuntimeError):
    """表示会话文件的读取、写入或定位操作失败。"""

    pass


@dataclass(slots=True)
class _CacheEntry:
    """按文件状态键缓存的已解析会话。"""

    mtime_ns: int
    size: int
    session: Session


class JsonSessionStore:
    """以原子替换方式保存会话，并隔离损坏的单个会话文件。

    ``list_sessions`` 以前每次调用都会完整解析目录下所有会话文件，
    界面每轮对话结束后会调用多次；这里按 ``(mtime, size)`` 缓存解析
    结果，文件未变化时直接复用。缓存对象只供列表展示使用；
    ``load`` 始终重新读盘，返回调用方可安全修改的独占对象。
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, _CacheEntry] = {}
        self._cache_lock = threading.Lock()

    @property
    def directory(self) -> Path:
        """返回当前会话文件所在的目录。"""
        return self._directory

    def save(self, session: Session) -> None:
        path = self._path_for(session.id)
        # 唯一临时文件名避免并发保存同一会话时互相覆盖。
        temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        content = json.dumps(session.to_dict(), ensure_ascii=False, indent=2)
        try:
            temporary_path.write_text(content, encoding="utf-8")
            # 同一文件系统内的替换避免目标文件只写入一部分内容。
            temporary_path.replace(path)
        except OSError as error:
            raise SessionStoreError(f"无法保存会话 {session.id}：{error}") from error
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def load(self, session_id: str) -> Session:
        """精确加载一个会话，始终返回独占的新对象。

        调用方（如 ``move_to_project``/``rename``）会原地修改返回值并保存，
        因此这里不能复用列表缓存中的共享对象，否则会把缓存里同一对象
        的其他引用一并污染。
        """
        resolved_id = self._resolve_id(session_id)
        path = self._path_for(resolved_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            session = Session.from_dict(data)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise SessionStoreError(f"无法读取会话 {resolved_id}：{error}") from error
        if session.id != resolved_id:
            raise SessionStoreError(f"会话文件名与内部 ID 不一致：{resolved_id}")
        return session

    def list_sessions(self) -> list[Session]:
        """列出全部会话；文件未变化时复用缓存解析结果。

        返回的 Session 对象可能来自缓存并被多个调用方共享，
        消费方只读元数据，不得原地修改。
        """
        sessions: list[Session] = []
        for path in self._directory.glob("*.json"):
            session_id = path.stem
            cached = self._cached(session_id, path)
            if cached is not None:
                sessions.append(cached)
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                session = Session.from_dict(data)
            except (OSError, json.JSONDecodeError, ValueError) as error:
                # 单个损坏文件不应拖垮整个列表，但需要留下诊断线索，
                # 否则会话"无声消失"很难排查。
                logger.warning("跳过无法解析的会话文件 %s：%s", path.name, error)
                continue
            if session.id != session_id:
                logger.warning("跳过 ID 不一致的会话文件：%s", path.name)
                continue
            self._store_cached(session_id, path, session)
            sessions.append(session)
        return sorted(
            sessions,
            key=lambda session: timestamp_sort_key(session.updated_at),
            reverse=True,
        )

    def latest(self) -> Session | None:
        sessions = self.list_sessions()
        return sessions[0] if sessions else None

    def delete(self, session_id: str) -> str:
        resolved_id = self._resolve_id(session_id)
        with self._cache_lock:
            self._cache.pop(resolved_id, None)
        try:
            self._path_for(resolved_id).unlink()
        except OSError as error:
            raise SessionStoreError(f"无法删除会话 {resolved_id}：{error}") from error
        return resolved_id

    def rename(self, session_id: str, title: str) -> Session:
        session = self.load(session_id)
        try:
            session.rename(title)
        except ValueError as error:
            raise SessionStoreError(str(error)) from error
        self.save(session)
        return session

    def move_to_project(
        self,
        session_id: str,
        project_id: str | None,
    ) -> Session:
        session = self.load(session_id)
        session.move_to_project(project_id)
        self.save(session)
        return session

    def _cached(self, session_id: str, path: Path) -> Session | None:
        """文件未变化时返回缓存会话，否则返回 None。"""
        try:
            stat = path.stat()
        except OSError:
            return None
        with self._cache_lock:
            entry = self._cache.get(session_id)
            if entry is None or entry.mtime_ns != stat.st_mtime_ns or entry.size != stat.st_size:
                return None
            return entry.session

    def _store_cached(self, session_id: str, path: Path, session: Session) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        with self._cache_lock:
            self._cache[session_id] = _CacheEntry(
                mtime_ns=stat.st_mtime_ns,
                size=stat.st_size,
                session=session,
            )

    def _resolve_id(self, session_id: str) -> str:
        value = session_id.strip().lower()
        if not value or any(character not in "0123456789abcdef" for character in value):
            raise SessionStoreError("会话 ID 格式错误")
        exact_path = self._path_for(value)
        if exact_path.exists():
            return value
        matches = [path.stem for path in self._directory.glob(f"{value}*.json")]
        if not matches:
            raise SessionStoreError(f"未找到会话：{session_id}")
        if len(matches) > 1:
            raise SessionStoreError(f"会话 ID 前缀不唯一：{session_id}")
        return matches[0]

    def _path_for(self, session_id: str) -> Path:
        return self._directory / f"{session_id}.json"
