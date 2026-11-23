"""受工作区限制的本地报告导出适配器。"""

import hashlib
import os
from uuid import uuid4

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
        resolved, _, _ = self._guard.prepare_text_write(path, "")
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
        resolved, created, data = self._guard.prepare_text_write(path, content)
        if not created:
            raise CareerDataError("目标文件已存在，请先核对导出记录；不会覆盖原文件。")
        temporary = resolved.with_name(f".{resolved.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            # 再校验一次路径；最终用硬链接排他发布完整文件，目标已存在时由文件系统拒绝。
            # 不允许退回 exists()+replace()，否则检查后新出现的文件仍可能被覆盖。
            current, created, _ = self._guard.prepare_text_write(path, content)
            if current != resolved or not created:
                raise CareerDataError("导出目标在写入前发生变化，未覆盖文件，请先核对。")
            os.link(temporary, resolved)
        except OSError as error:
            raise WorkspaceAccessError(
                "无法安全创建报告文件；不会退回覆盖写入，请核对导出记录。"
            ) from error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return ExportArtifact(self._guard.relative_path(resolved), len(data))
