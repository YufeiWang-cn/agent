"""求职应用入口：统一命令、授权前置条件、来源输入和业务用例调用。"""

from .commands import (
    BuildResumeDraft,
    CareerCommand,
    DecideReview,
    ExportReview,
    ForgetFact,
    GetReview,
    ImportDocument,
    RecallMemory,
    SaveFact,
    SaveReview,
    requires_confirmation,
)
from .domain import CareerDataError, Json, text
from .ports import DocumentSource, ReportExporter
from .use_cases import CareerUseCases


class CareerApplication:
    def __init__(
        self, use_cases: CareerUseCases, reader: DocumentSource, exporter: ReportExporter
    ) -> None:
        self._use_cases = use_cases
        self._reader = reader
        self._exporter = exporter

    def execute(
        self, command: CareerCommand, *, user_message: str = "", confirmed: bool = False
    ) -> Json:
        """confirmed 仅表示本地入口已完成确认，不是远程认证凭据。"""
        if requires_confirmation(command) and not confirmed:
            raise CareerDataError("此操作需要用户确认，尚未执行。")
        if isinstance(command, ImportDocument):
            return self._import(command, user_message)
        if isinstance(command, RecallMemory):
            if command.document_id:
                return self._use_cases.document(text(command.document_id, "document_id", 64))
            if not isinstance(command.query, str) or len(command.query) > 2000:
                raise CareerDataError("query 必须是最多 2000 字符的文本。")
            return self._use_cases.snapshot(command.query, limit=command.limit)
        if isinstance(command, SaveFact):
            return self._use_cases.save_fact(
                key=command.key,
                category=command.category,
                statement=command.statement,
                source_id=text(command.source_id, "source_id", 64),
                source_quote=command.source_quote,
                current_user=user_message,
            )
        if isinstance(command, ForgetFact):
            self._use_cases.forget_fact(text(command.fact_id, "fact_id", 64))
            return dict(deleted=True)
        if isinstance(command, SaveReview):
            return self._use_cases.save_review(
                resume_id=text(command.resume_id, "resume_id", 64),
                job_id=text(command.job_id, "job_id", 64) if command.job_id is not None else None,
                title=command.title,
                markdown=command.markdown,
                changes=command.changes,
            )
        if isinstance(command, GetReview):
            return self._use_cases.review(text(command.review_id, "review_id", 64))
        if isinstance(command, DecideReview):
            return self._use_cases.decide_review(
                text(command.review_id, "review_id", 64), command.decision
            )
        if isinstance(command, ExportReview):
            review_id = text(command.review_id, "review_id", 64)
            path = text(command.path, "path", 500)
            if not path.lower().endswith(".md"):
                raise CareerDataError("导出路径必须以 .md 结尾。")
            content = self._use_cases.render_review(review_id)
            artifact = self._exporter.write_new(path, content)
            return dict(
                path=artifact.path,
                bytes=artifact.bytes,
                status=self._use_cases.review(review_id)["status"],
            )
        if isinstance(command, BuildResumeDraft):
            return self._use_cases.build_resume_draft(command.fact_ids)
        raise CareerDataError("不支持的求职命令。")

    def _import(self, command: ImportDocument, user_message: str) -> Json:
        if (command.text is None) == (command.path is None):
            raise CareerDataError("text 与 path 必须且只能提供一个。")
        if command.path is not None:
            path = text(command.path, "path", 500)
            if path not in user_message:
                raise CareerDataError("文件路径必须由本轮用户明确提供。")
            content = self._reader.read(path)
        else:
            content = text(command.text, "text", 50_000)
            if content not in user_message:
                raise CareerDataError("导入文本必须逐字来自本轮用户输入，不能导入模型生成的经历。")
        return self._use_cases.import_document(command.kind, command.title, content)
