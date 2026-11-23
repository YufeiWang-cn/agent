"""求职应用入口：统一命令、授权前置条件、来源输入和业务用例调用。"""

from .commands import (
    BuildResumeDraft,
    CheckExport,
    ListExports,
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
from .domain import CareerDataError, ExportUnconfirmedError, Json, text
from ..timekeeping import now_china
from .ports import DocumentSource, ReportExporter, ExportTarget
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
                findings=command.findings,
                general_advice=command.general_advice,
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
            return self._export_review(review_id, path)
        if isinstance(command, ListExports):
            filter_review_id = (
                text(command.review_id, "review_id", 64) if command.review_id is not None else None
            )
            return dict(exports=self._use_cases.list_exports(filter_review_id), limit=50)
        if isinstance(command, CheckExport):
            return self._check_export(text(command.export_id, "export_id", 64))
        if isinstance(command, BuildResumeDraft):
            return self._use_cases.build_resume_draft(command.fact_ids)
        raise CareerDataError("不支持的求职命令。")

    def _export_review(self, review_id: str, path: str) -> Json:
        target = self._exporter.prepare_target(path)
        record, content = self._use_cases.prepare_export(review_id, target)
        try:
            artifact = self._exporter.write_new(target.path, content)
            if artifact.path != target.path or artifact.bytes != record["byte_count"]:
                raise CareerDataError("导出返回值与准备记录不一致。")
        except Exception as error:
            # 适配器可能在实际写出后抛错，不能据异常断言文件不存在。
            try:
                self._use_cases.finish_export(record["id"], "unconfirmed", type(error).__name__)
            except Exception:
                pass  # 意图已落盘；状态更新失败时保留 pending 供核对。
            raise ExportUnconfirmedError(
                f"导出未确认完成，记录 ID：{record['id']}。请先核对结果，不要直接重试。"
            ) from error
        try:
            self._use_cases.finish_export(record["id"], "succeeded")
        except Exception as error:
            raise ExportUnconfirmedError(
                f"文件已写出，但完成记录未保存，记录 ID：{record['id']}。请先核对结果。"
            ) from error
        return dict(
            path=artifact.path,
            bytes=artifact.bytes,
            status=record["review_status"],
            export_id=record["id"],
            export_result="succeeded",
        )

    def _check_export(self, export_id: str) -> Json:
        record = self._use_cases.export_record(export_id)
        observation = self._exporter.inspect(ExportTarget(record["path"], record["workspace_id"]))
        state = observation["state"]
        if state == "present":
            state = (
                "match"
                if (
                    observation["sha256"] == record["content_sha256"]
                    and observation["bytes"] == record["byte_count"]
                )
                else "modified"
            )
        review = self._use_cases.review(record["review_id"])
        messages = {
            "match": "当前文件与本次导出快照一致；这不改变历史执行记录或证明报告仍然有效。",
            "missing": "当前未找到文件；本次核对不会重新导出。",
            "modified": "当前文件内容与导出快照不一致；不会覆盖文件。",
            "unreadable": "当前无法安全读取文件，不能确认结果。",
            "workspace_mismatch": "当前工作区与导出时不同，未读取该相对路径。",
        }
        return dict(
            record=record,
            file_status=state,
            checked_at=now_china().isoformat(),
            observed_sha256=observation.get("sha256"),
            observed_bytes=observation.get("bytes"),
            current_review_status=review["status"],
            review_stale=review["stale"],
            stale_reasons=review["stale_reasons"],
            message=messages[state],
        )

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
