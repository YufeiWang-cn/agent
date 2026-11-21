"""兼容既有 Agent API；具体依赖装配和通用运行时分别维护。"""

import logging
from typing import cast

from .bootstrap import assemble_runtime
from .career.service import CareerService
from .career.application import CareerApplication
from .career.commands import ForgetFact, RecallMemory
from .config import PROJECT_ROOT, Settings
from .context import ContextManager
from .journal import RunJournal
from .memory import JsonProjectStore, JsonSessionStore
from .models import ChatModel
from .observability import RuntimeMetrics
from .permissions import ToolConfirmer
from .reliability import RetryPolicy
from .runtime.engine import AgentCancelledError, AgentRuntime
from .timekeeping import now_china
from .tools import ToolRegistry

__all__ = ["Agent", "AgentCancelledError"]


class Agent(AgentRuntime):
    """保留 CLI、Tk 和现有扩展使用的构造参数与公共方法。"""

    def __init__(
        self,
        settings: Settings,
        model: ChatModel | None = None,
        tools: ToolRegistry | None = None,
        session_store: JsonSessionStore | None = None,
        project_store: JsonProjectStore | None = None,
        context_manager: ContextManager | None = None,
        confirmer: ToolConfirmer | None = None,
        retry_policy: RetryPolicy | None = None,
        metrics: RuntimeMetrics | None = None,
        logger: logging.Logger | None = None,
        run_journal: RunJournal | None = None,
    ) -> None:
        super().__init__(
            assemble_runtime(
                settings,
                project_root=PROJECT_ROOT,
                clock=lambda: now_china(),
                model=model,
                tools=tools,
                session_store=session_store,
                project_store=project_store,
                context_manager=context_manager,
                confirmer=confirmer,
                retry_policy=retry_policy,
                metrics=metrics,
                logger=logger,
                run_journal=run_journal,
            )
        )

    @property
    def _career(self) -> CareerService | None:
        """兼容旧扩展；新入口应调用应用层接口。"""
        return cast(CareerService | None, self._business)

    @property
    def career_enabled(self) -> bool:
        return self._career is not None

    @property
    def career_application(self) -> CareerApplication:
        if self._career is None:
            raise ValueError("当前不是求职模式，请使用 career.py 或 AGENT_MODE=career。")
        return self._career.application

    def career_snapshot(self) -> dict[str, object]:
        return self.career_application.execute(RecallMemory(limit=200))

    def forget_career_fact(self, fact_id: str) -> None:
        """兼容原有已确认的本地调用；界面与工具均使用同一个应用用例。"""
        self.career_application.execute(ForgetFact(fact_id), confirmed=True)
