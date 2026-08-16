"""导出工作区安全边界及其访问异常。"""

from .guard import WorkspaceAccessError, WorkspaceGuard

__all__ = ["WorkspaceAccessError", "WorkspaceGuard"]
