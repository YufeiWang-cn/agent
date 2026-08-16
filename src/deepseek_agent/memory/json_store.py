"""使用独立 JSON 文件持久化会话，并支持短 ID 查找。"""

import json
from pathlib import Path

from .session import Session


class SessionStoreError(RuntimeError):
    """表示会话文件的读取、写入或定位操作失败。"""

    pass


class JsonSessionStore:
    """以原子替换方式保存会话，并隔离损坏的单个会话文件。"""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)

    @property
    def directory(self) -> Path:
        """返回当前会话文件所在的目录。"""
        return self._directory

    def save(self, session: Session) -> None:
        path = self._path_for(session.id)
        temporary_path = path.with_suffix(".tmp")
        content = json.dumps(session.to_dict(), ensure_ascii=False, indent=2)
        temporary_path.write_text(content, encoding="utf-8")
        # 同一文件系统内的替换避免目标文件只写入一部分内容。
        temporary_path.replace(path)

    def load(self, session_id: str) -> Session:
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
        sessions: list[Session] = []
        for path in self._directory.glob("*.json"):
            try:
                sessions.append(self.load(path.stem))
            except SessionStoreError:
                continue
        return sorted(sessions, key=lambda session: session.updated_at, reverse=True)

    def latest(self) -> Session | None:
        sessions = self.list_sessions()
        return sessions[0] if sessions else None

    def delete(self, session_id: str) -> str:
        resolved_id = self._resolve_id(session_id)
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
