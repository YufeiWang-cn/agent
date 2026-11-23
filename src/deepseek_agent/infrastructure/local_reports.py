"""受工作区限制的本地报告导出适配器。"""

import hashlib
import os

from ..career.domain import CareerDataError, Json
from ..career.ports import ExportArtifact, ExportTarget
from ..workspace import WorkspaceGuard, WorkspaceAccessError


class LocalReportExporter:
    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    @property
    def workspace_id(self) -> str:
        # 只保存工作区身份摘要，核对时不能用同名相对路径误读另一个工作区。
        return hashlib.sha256(os.path.normcase(str(self._guard.root)).encode("utf-8")).hexdigest()

    def prepare_target(self, path: str) -> ExportTarget:
        resolved, created, _ = self._guard.prepare_text_write(path, "")
        if not created:
            raise CareerDataError("目标文件已存在，请选择新文件名；不会覆盖原文件。")
        return ExportTarget(self._guard.relative_path(resolved), self.workspace_id)

    def inspect(self, target: ExportTarget) -> Json:
        if target.workspace_id != self.workspace_id:
            return dict(state="workspace_mismatch")
        try:
            _, missing, _ = self._guard.prepare_text_write(target.path, "")
            if missing:
                return dict(state="missing")
            _, data = self._guard.read_bytes(target.path)
            return dict(state="present", sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
        except (WorkspaceAccessError, OSError):
            # 拒绝路径、非普通文件、超限和无读取权限都不能假装已匹配。
            return dict(state="unreadable")

    def write_new(self, path: str, content: str) -> ExportArtifact:
        _, created, _ = self._guard.prepare_text_write(path, content)
        if not created:
            raise CareerDataError("目标文件已存在，请选择新文件名；不会覆盖原文件。")
        resolved, _, count = self._guard.write_text(path, content)
        return ExportArtifact(self._guard.relative_path(resolved), count)
