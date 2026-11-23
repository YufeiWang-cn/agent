"""模型工具协议与业务命令之间的映射及求职工具白名单。"""

from typing import TYPE_CHECKING

from ..tools import ToolRegistry
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
)
from .domain import CareerDataError, Json
from .network_tools import CareerNetworkTool

if TYPE_CHECKING:
    from .service import CareerService


def command_from_arguments(name: str, arguments: Json) -> CareerCommand:
    if name == "import_career_document":
        return ImportDocument(
            arguments.get("kind", ""),
            arguments.get("title", ""),
            arguments.get("text"),
            arguments.get("path"),
        )
    if name == "career_memory":
        action = arguments.get("action")
        if action == "recall":
            return RecallMemory(arguments.get("query", ""), arguments.get("document_id"))
        if action == "save":
            return SaveFact(
                arguments.get("key", ""),
                arguments.get("category", ""),
                arguments.get("statement", ""),
                arguments.get("source_id", ""),
                arguments.get("source_quote", ""),
            )
        if action == "forget":
            return ForgetFact(arguments.get("fact_id", ""))
    elif name == "career_review":
        action = arguments.get("action")
        if action == "save":
            return SaveReview(
                arguments.get("resume_id", ""),
                arguments.get("job_id"),
                arguments.get("title", ""),
                arguments.get("markdown", ""),
                arguments.get("changes", []),
                arguments.get("findings"),
                arguments.get("general_advice"),
            )
        if action == "exports":
            return ListExports(arguments.get("review_id"))
        if action == "check_export":
            return CheckExport(arguments.get("export_id", ""))
        if action == "get":
            return GetReview(arguments.get("review_id", ""))
        if action == "decide":
            return DecideReview(arguments.get("review_id", ""), arguments.get("decision", ""))
    elif name == "export_career_review":
        return ExportReview(arguments.get("review_id", ""), arguments.get("path", ""))
    elif name == "build_resume_draft":
        return BuildResumeDraft(arguments.get("fact_ids", []))
    raise CareerDataError("action 无效。")


def build_career_registry(service: "CareerService", existing: ToolRegistry) -> ToolRegistry:
    from .tools import (
        BuildResumeDraftTool,
        CareerMemoryTool,
        CareerReviewTool,
        ExportCareerReviewTool,
        ImportCareerDocumentTool,
    )

    names = {"update_plan", "get_current_time", "calculator", "web_search", "read_web_page"}
    safe_tools = [
        CareerNetworkTool(existing.get(name))
        if name in {"web_search", "read_web_page"}
        else existing.get(name)
        for name in existing.names
        if name in names
    ]
    return ToolRegistry(
        [
            *safe_tools,
            ImportCareerDocumentTool(service),
            CareerMemoryTool(service),
            CareerReviewTool(service),
            ExportCareerReviewTool(service),
            BuildResumeDraftTool(service),
        ]
    )
