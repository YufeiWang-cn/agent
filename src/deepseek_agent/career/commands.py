"""界面和模型工具共用的业务命令；不包含模型、SQL 或 Tk 对象。"""

from dataclasses import dataclass

from .domain import Json


@dataclass(frozen=True)
class ImportDocument:
    kind: str
    title: str
    text: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class RecallMemory:
    query: str = ""
    document_id: str | None = None
    limit: int = 20


@dataclass(frozen=True)
class SaveFact:
    key: str
    category: str
    statement: str
    source_id: str
    source_quote: str


@dataclass(frozen=True)
class ForgetFact:
    fact_id: str


@dataclass(frozen=True)
class SaveReview:
    resume_id: str
    job_id: str | None
    title: str
    markdown: str
    changes: list[Json]


@dataclass(frozen=True)
class GetReview:
    review_id: str


@dataclass(frozen=True)
class DecideReview:
    review_id: str
    decision: str


@dataclass(frozen=True)
class ExportReview:
    review_id: str
    path: str


@dataclass(frozen=True)
class BuildResumeDraft:
    fact_ids: list[str]


CareerCommand = (
    ImportDocument
    | RecallMemory
    | SaveFact
    | ForgetFact
    | SaveReview
    | GetReview
    | DecideReview
    | ExportReview
    | BuildResumeDraft
)


def requires_confirmation(command: CareerCommand) -> bool:
    return isinstance(command, (SaveFact, ForgetFact, DecideReview, ExportReview))


def is_read_only(command: CareerCommand) -> bool:
    return isinstance(command, (RecallMemory, GetReview))
