"""受工作区限制的本地报告导出适配器。"""

from ..career.domain import CareerDataError
from ..career.ports import ExportArtifact
from ..workspace import WorkspaceGuard


class LocalReportExporter:
    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def write_new(self, path: str, content: str) -> ExportArtifact:
        _, created, _ = self._guard.prepare_text_write(path, content)
        if not created:
            raise CareerDataError("目标文件已存在，请选择新文件名；不会覆盖原文件。")
        resolved, _, count = self._guard.write_text(path, content)
        return ExportArtifact(self._guard.relative_path(resolved), count)
