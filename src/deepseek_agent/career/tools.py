"""具有来源校验和用户确认边界的求职工具。"""

import json
from typing import TYPE_CHECKING

from ..tools import JsonObject, Tool, ToolEffect, ToolExecutionError
from ..workspace import WorkspaceAccessError
from .store import CareerDataError, text

if TYPE_CHECKING:
    from .service import CareerService


def schema(properties: JsonObject, required: list[str]) -> JsonObject:
    return dict(type="object", properties=properties, required=required, additionalProperties=False)


def string(description: str) -> JsonObject:
    return dict(type="string", description=description)


class CareerTool(Tool):
    effect = ToolEffect.REVERSIBLE_WRITE

    def __init__(self, service: "CareerService") -> None:
        self.service = service

    def execute(self, arguments: JsonObject) -> str:
        try:
            return json.dumps(self.run(arguments), ensure_ascii=False)
        except (CareerDataError, WorkspaceAccessError) as error:
            raise ToolExecutionError(str(error)) from error

    def run(self, arguments: JsonObject) -> JsonObject:
        raise NotImplementedError


class ImportCareerDocumentTool(CareerTool):
    name = "import_career_document"
    description = (
        "导入用户提供的简历或 JD，保存不可变来源；不会自动确认其中的经历。文件须在工作区内。"
    )
    parameters = schema(
        {
            "kind": dict(type="string", enum=["resume", "job"]),
            "title": string("来源名称"),
            "text": string("逐字来自本轮用户输入的简历或 JD 文本；与 path 二选一"),
            "path": string("本轮用户指定的工作区相对文件路径；支持 txt/md/pdf/docx"),
        },
        ["kind", "title"],
    )

    def run(self, arguments: JsonObject) -> JsonObject:
        inline = arguments.get("text")
        path = arguments.get("path")
        if (inline is None) == (path is None):
            raise CareerDataError("text 与 path 必须且只能提供一个。")
        if path is not None:
            path = text(path, "path", 500)
            if path not in self.service.current_user:
                raise CareerDataError("文件路径必须由本轮用户明确提供。")
            content = self.service.reader.read(path)
        else:
            content = text(inline, "text", 50_000)
            if content not in self.service.current_user:
                raise CareerDataError("导入文本必须逐字来自本轮用户输入，不能导入模型生成的经历。")
        return self.service.store.import_document(
            arguments.get("kind", ""), arguments.get("title", ""), content
        )


class CareerMemoryTool(CareerTool):
    name = "career_memory"
    description = (
        "查询长期档案，或经用户确认保存／纠正事实。同一事项沿用相同 key；草稿不能自动成为事实。"
    )
    confirmation_description = (
        "请核对将保存或删除的经历事实与来源原话。确认保存后，该事实会在新会话中复用。"
    )
    parameters = schema(
        {
            "action": dict(type="string", enum=["recall", "save", "forget"]),
            "query": string("查询词"),
            "document_id": string("读取完整来源文档的 ID"),
            "key": string("稳定事项名称，如 agent项目/职责；纠正时必须沿用旧 key"),
            "category": dict(
                type="string", enum=["education", "project", "skill", "preference", "feedback"]
            ),
            "statement": string("供用户确认的真实事实"),
            "source_id": string("resume 来源 ID 或 current_user；不能引用 job"),
            "source_quote": string("逐字来自来源或本轮用户输入的原话"),
            "fact_id": string("要删除的事实 ID"),
        },
        ["action"],
    )

    def requires_confirmation_for(self, arguments: JsonObject) -> bool:
        return arguments.get("action") != "recall"

    def effect_for(self, arguments: JsonObject) -> ToolEffect:
        return ToolEffect.READ_ONLY if arguments.get("action") == "recall" else self.effect

    def run(self, arguments: JsonObject) -> JsonObject:
        action = arguments.get("action")
        if action == "recall":
            if arguments.get("document_id"):
                return self.service.store.document(
                    text(arguments["document_id"], "document_id", 64)
                )
            query = arguments.get("query", "")
            if not isinstance(query, str) or len(query) > 2000:
                raise CareerDataError("query 必须是最多 2000 字符的文本。")
            return self.service.store.snapshot(query)
        if action == "save":
            return self.service.store.save_fact(
                key=arguments.get("key", ""),
                category=arguments.get("category", ""),
                statement=arguments.get("statement", ""),
                source_id=text(arguments.get("source_id"), "source_id", 64),
                source_quote=arguments.get("source_quote", ""),
                current_user=self.service.current_user,
            )
        if action == "forget":
            self.service.store.forget_fact(text(arguments.get("fact_id"), "fact_id", 64))
            return dict(deleted=True)
        raise CareerDataError("action 无效。")


class CareerReviewTool(CareerTool):
    name = "career_review"
    description = (
        "保存诊断／岗位分析和逐条修改草稿，读取报告，或经用户确认接受／拒绝。修改须引用确认事实。"
    )
    confirmation_description = "确认接受或拒绝这份报告。接受不会修改经历事实或覆盖原简历。"
    parameters = schema(
        {
            "action": dict(type="string", enum=["save", "get", "decide"]),
            "resume_id": string("原始简历来源 ID"),
            "job_id": string("可选 JD 来源 ID"),
            "title": string("报告名称"),
            "markdown": string("诊断、岗位分析或面试反馈正文；待补信息明确标注"),
            "changes": dict(
                type="array",
                maxItems=30,
                items=schema(
                    {
                        "original": string("来源简历中的逐字原文"),
                        "suggested": string("有确认事实依据的建议文本"),
                        "reason": string("修改理由"),
                        "evidence_fact_ids": dict(
                            type="array", minItems=1, maxItems=20, items=dict(type="string")
                        ),
                    },
                    ["original", "suggested", "reason", "evidence_fact_ids"],
                ),
            ),
            "review_id": string("报告 ID"),
            "decision": dict(type="string", enum=["accepted", "rejected"]),
        },
        ["action"],
    )

    def requires_confirmation_for(self, arguments: JsonObject) -> bool:
        return arguments.get("action") == "decide"

    def effect_for(self, arguments: JsonObject) -> ToolEffect:
        return ToolEffect.READ_ONLY if arguments.get("action") == "get" else self.effect

    def run(self, arguments: JsonObject) -> JsonObject:
        action = arguments.get("action")
        if action == "save":
            job_id = arguments.get("job_id")
            if job_id is not None:
                job_id = text(job_id, "job_id", 64)
            return self.service.store.save_review(
                resume_id=text(arguments.get("resume_id"), "resume_id", 64),
                job_id=job_id,
                title=arguments.get("title", ""),
                markdown=arguments.get("markdown", ""),
                changes=arguments.get("changes", []),
            )
        review_id = text(arguments.get("review_id"), "review_id", 64)
        if action == "get":
            return self.service.store.review(review_id)
        if action == "decide":
            return self.service.store.decide_review(review_id, arguments.get("decision", ""))
        raise CareerDataError("action 无效。")


class ExportCareerReviewTool(CareerTool):
    name = "export_career_review"
    description = (
        "经用户确认将报告导出为新 Markdown 文件，包含状态、逐条修改及事实依据；不覆盖已有文件。"
    )
    requires_confirmation = True
    parameters = schema(
        {"review_id": string("报告 ID"), "path": string("新的工作区相对 .md 路径；父目录须已存在")},
        ["review_id", "path"],
    )

    def run(self, arguments: JsonObject) -> JsonObject:
        review_id = text(arguments.get("review_id"), "review_id", 64)
        path = text(arguments.get("path"), "path", 500)
        if not path.lower().endswith(".md"):
            raise CareerDataError("导出路径必须以 .md 结尾。")
        content = self.service.store.render_review(review_id)
        _, created, _ = self.service.guard.prepare_text_write(path, content)
        if not created:
            raise CareerDataError("目标文件已存在，请选择新文件名；不会覆盖原文件。")
        resolved, _, count = self.service.guard.write_text(path, content)
        return dict(
            path=self.service.guard.relative_path(resolved),
            bytes=count,
            status=self.service.store.review(review_id)["status"],
        )


class BuildResumeDraftTool(CareerTool):
    name = "build_resume_draft"
    description = "为没有简历的学生将用户已确认的事实组织为初稿。只使用事实原文，草稿不能作为新增个人事实来源。"
    parameters = schema(
        {"fact_ids": dict(type="array", minItems=1, maxItems=100, items=dict(type="string"))},
        ["fact_ids"],
    )

    def run(self, arguments: JsonObject) -> JsonObject:
        return self.service.store.build_resume_draft(arguments.get("fact_ids", []))
