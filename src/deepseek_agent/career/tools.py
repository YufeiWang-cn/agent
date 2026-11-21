"""具有来源校验和用户确认边界的求职工具。"""

import json
from typing import TYPE_CHECKING

from ..tools import JsonObject, Tool, ToolEffect, ToolExecutionError
from ..workspace import WorkspaceAccessError
from .domain import CareerDataError
from .commands import is_read_only, requires_confirmation
from .tool_adapters import command_from_arguments

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

    def requires_confirmation_for(self, arguments: JsonObject) -> bool:
        return requires_confirmation(command_from_arguments(self.name, arguments))

    def effect_for(self, arguments: JsonObject) -> ToolEffect:
        command = command_from_arguments(self.name, arguments)
        return ToolEffect.READ_ONLY if is_read_only(command) else self.effect

    def run(self, arguments: JsonObject) -> JsonObject:
        # ToolExecutor 是工具调用的授权边界；到达 execute 时确认已经完成。
        return self.service.application.execute(
            command_from_arguments(self.name, arguments),
            user_message=self.service.run_context.user_message,
            confirmed=True,
        )


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


class CareerReviewTool(CareerTool):
    name = "career_review"
    description = (
        "保存结构化分析和逐条修改草稿，读取报告，或经用户确认接受／拒绝。"
        "save 必须提供 findings（可为空）；个人经历判断与修改须引用确认事实。"
    )
    confirmation_description = "确认接受或拒绝这份报告。接受不会修改经历事实或覆盖原简历。"
    parameters = schema(
        {
            "action": dict(type="string", enum=["save", "get", "decide"]),
            "resume_id": string("原始简历来源 ID"),
            "job_id": string("可选 JD 来源 ID"),
            "title": string("报告名称"),
            "findings": dict(
                type="array",
                maxItems=30,
                description="save 必填。个人经历和能力判断，每条引用事实；无判断时传空数组。",
                items=schema(
                    {
                        "text": string("有事实依据的分析，不得夸大来源"),
                        "evidence_fact_ids": dict(
                            type="array", minItems=1, maxItems=20, items=dict(type="string")
                        ),
                    },
                    ["text", "evidence_fact_ids"],
                ),
            ),
            "general_advice": dict(
                type="array",
                maxItems=20,
                items=dict(type="string"),
                description="一般方法建议与待补问题；不得在此陈述或推断个人经历。",
            ),
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


class BuildResumeDraftTool(CareerTool):
    name = "build_resume_draft"
    description = "为没有简历的学生将用户已确认的事实组织为初稿。只使用事实原文，草稿不能作为新增个人事实来源。"
    parameters = schema(
        {"fact_ids": dict(type="array", minItems=1, maxItems=100, items=dict(type="string"))},
        ["fact_ids"],
    )
