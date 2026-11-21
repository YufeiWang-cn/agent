"""组装求职工具和每轮刷新、独立于聊天历史的长期记忆。"""

from dataclasses import replace
from pathlib import Path

from ..runtime.contracts import RunContext
from ..tools import ToolRegistry
from ..workspace import WorkspaceGuard
from .documents import DocumentReader
from .context import CareerContextBuilder
from .application import CareerApplication
from .tool_adapters import build_career_registry
from ..infrastructure.local_reports import LocalReportExporter
from .use_cases import CareerUseCases


class CareerService:
    def __init__(self, store: CareerUseCases, workspace: Path, max_file_size: int) -> None:
        self.store = store
        self.reader = DocumentReader(workspace)
        self.guard = WorkspaceGuard(workspace, max_file_size)
        self.application = CareerApplication(store, self.reader, LocalReportExporter(self.guard))
        self._run_context = RunContext("", "", "", "", "")
        self.policy = (Path(__file__).parents[1] / "prompts" / "career.md").read_text(
            encoding="utf-8"
        )
        # 运行时上下文每个模型步骤都会拼进系统提示，但内容只在
        # 本轮用户输入或档案数据变化时才变；按 (数据版本, 用户输入) 缓存。
        self._context_cache_key: tuple[int, str] | None = None
        self._context_cache_value = ""

    def bind_turn(self, context: RunContext) -> None:
        self._run_context = context

    @property
    def run_context(self) -> RunContext:
        return self._run_context

    @property
    def current_user(self) -> str:
        """旧接口兼容：这里表示本轮原话，而非登录身份。"""
        return self._run_context.user_message

    @current_user.setter
    def current_user(self, value: str) -> None:
        self._run_context = replace(self._run_context, user_message=value)

    def runtime_context(self) -> str:
        cache_key = (self.store.generation, self.current_user)
        if self._context_cache_key == cache_key:
            return self._context_cache_value
        self._context_cache_key = cache_key
        self._context_cache_value = self._build_runtime_context()
        return self._context_cache_value

    def registry(self, existing: ToolRegistry) -> ToolRegistry:
        return build_career_registry(self, existing)

    def _build_runtime_context(self) -> str:
        return CareerContextBuilder(self.store, self.policy).build(self.run_context.user_message)
