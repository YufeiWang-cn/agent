"""协调对话内存、会话持久化和项目归属变更。"""

from ..conversation import Conversation, Message
from ..planning import TaskPlan
from .json_store import JsonSessionStore
from .project import Project
from .project_store import JsonProjectStore, ProjectStoreError
from .session import Session


class SessionCoordinator:
    """维护当前会话，并为跨存储操作提供一致性边界。"""

    def __init__(
        self,
        conversation: Conversation,
        session_store: JsonSessionStore,
        project_store: JsonProjectStore,
    ) -> None:
        self._conversation = conversation
        self._session_store = session_store
        self._project_store = project_store
        self._current = self._restore_latest_session()

    @property
    def current(self) -> Session:
        """返回当前会话快照。"""
        return self._current

    def list_sessions(self) -> list[Session]:
        return self._session_store.list_sessions()

    def list_projects(self) -> list[Project]:
        return self._project_store.list_projects()

    def create_project(self, name: str) -> Project:
        return self._project_store.create(name)

    def rename_project(self, project_id: str, name: str) -> Project:
        return self._project_store.rename(project_id, name)

    def delete_project(
        self,
        project_id: str,
        plan: TaskPlan | None,
    ) -> Project:
        """删除项目，并在失败时补偿恢复所有会话的原项目归属。"""
        project = self._project_store.get(project_id)
        self.save_current(plan)
        # 完整会话快照是补偿依据，可同时恢复项目归属和其他会话字段。
        affected = [
            session
            for session in self._session_store.list_sessions()
            if session.project_id == project.id
        ]
        moved_sessions: dict[str, Session] = {}
        try:
            # JSON 存储没有跨文件事务，因此先移动会话，再删除空项目。
            for session in affected:
                moved_sessions[session.id] = self._session_store.move_to_project(
                    session.id,
                    None,
                )
            deleted = self._project_store.delete(project.id)
        except Exception as error:
            self._restore_session_snapshots(affected, error)
            raise

        # 所有磁盘操作成功后，才提交当前会话的项目归属。
        if self._current.id in moved_sessions:
            self._current = moved_sessions[self._current.id]
        return deleted

    def start_new_session(
        self,
        plan: TaskPlan | None,
        project_id: str | None = None,
    ) -> Session:
        """保存当前会话后创建新会话，并在成功后切换内存状态。"""
        resolved_project_id = self._resolve_project_id(project_id)
        self.save_current(plan)
        return self._start_new_session(resolved_project_id)

    def clear_conversation(self) -> Session:
        """持久化一个空会话快照，并在成功后清空对话内存。"""
        cleared_messages = self._initial_messages()
        candidate = self._copy_session(self._current)
        candidate.update_messages(cleared_messages)
        candidate.update_plan(None)
        self._session_store.save(candidate)
        self._conversation.restore(cleared_messages)
        self._current = candidate
        return candidate

    def load_session(
        self,
        session_id: str,
        current_plan: TaskPlan | None,
    ) -> Session:
        """保存当前会话，再加载并提交目标会话。"""
        self.save_current(current_plan)
        session = self._session_store.load(session_id)
        # 对话结构验证必须先于当前会话指针的切换。
        self._conversation.restore(session.messages)
        self._current = session
        return session

    def delete_session(
        self,
        session_id: str,
        replacement_project_id: str | None = None,
    ) -> str:
        """删除会话，并为被删除的当前会话准备可用替代项。"""
        target = self._session_store.load(session_id)
        if target.id != self._current.id:
            return self._session_store.delete(target.id)

        resolved_project_id = self._resolve_project_id(replacement_project_id)
        # 替代会话先落盘，确保旧会话删除后仍有可用的当前会话。
        replacement = self._create_new_session(resolved_project_id)
        try:
            deleted_id = self._session_store.delete(target.id)
        except Exception:
            # 删除旧会话失败时，尽量移除尚未启用的替代会话。
            try:
                self._session_store.delete(replacement.id)
            except Exception:
                pass
            raise

        self._conversation.restore(replacement.messages)
        self._current = replacement
        return deleted_id

    def rename_session(
        self,
        session_id: str,
        title: str,
        current_plan: TaskPlan | None,
    ) -> Session:
        self.save_current(current_plan)
        session = self._session_store.rename(session_id, title)
        if session.id == self._current.id:
            self._current = session
        return session

    def move_session(
        self,
        session_id: str,
        project_id: str | None,
        current_plan: TaskPlan | None,
    ) -> Session:
        self.save_current(current_plan)
        resolved_project_id = self._resolve_project_id(project_id)
        session = self._session_store.move_to_project(
            session_id,
            resolved_project_id,
        )
        if session.id == self._current.id:
            self._current = session
        return session

    def save_current(self, plan: TaskPlan | None) -> None:
        """先保存候选副本，成功后再替换当前会话快照。"""
        candidate = self._copy_session(self._current)
        if candidate.title == "新会话":
            first_user_message = next(
                (
                    message.get("content", "")
                    for message in self._conversation.visible_history()
                    if message.get("role") == "user"
                ),
                "",
            )
            if first_user_message:
                compact_title = " ".join(str(first_user_message).split())
                candidate.title = compact_title[:30]
        candidate.update_messages(self._conversation.messages)
        candidate.update_plan(plan)
        self._session_store.save(candidate)
        self._current = candidate

    def _restore_latest_session(self) -> Session:
        latest = self._session_store.latest()
        if latest is not None:
            try:
                self._conversation.restore(latest.messages)
                return latest
            except ValueError:
                pass
        return self._start_new_session()

    def _start_new_session(self, project_id: str | None = None) -> Session:
        session = self._create_new_session(project_id)
        self._conversation.restore(session.messages)
        self._current = session
        return session

    def _create_new_session(self, project_id: str | None = None) -> Session:
        session = Session.create(self._initial_messages(), project_id=project_id)
        self._session_store.save(session)
        return session

    def _resolve_project_id(self, project_id: str | None) -> str | None:
        if project_id is None:
            return None
        return self._project_store.get(project_id).id

    def _initial_messages(self) -> list[Message]:
        return [{"role": "system", "content": self._conversation.system_prompt}]

    def _restore_session_snapshots(
        self,
        snapshots: list[Session],
        original_error: Exception,
    ) -> None:
        """恢复补偿快照，并显式报告无法完整回滚的风险。"""
        rollback_errors: list[str] = []
        for snapshot in snapshots:
            try:
                self._session_store.save(snapshot)
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            details = "；".join(rollback_errors)
            raise ProjectStoreError(
                f"删除项目失败，且会话归属回滚失败：{details}"
            ) from original_error

    @staticmethod
    def _copy_session(session: Session) -> Session:
        """生成深拷贝候选对象，避免持久化前修改当前会话。"""
        return Session.from_dict(session.to_dict())
