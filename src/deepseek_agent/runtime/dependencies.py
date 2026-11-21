"""已装配的运行依赖。具体实现由应用装配入口选择。"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ..context import ContextManager
from ..conversation import Conversation
from ..journal import RunJournal
from ..memory import SessionCoordinator
from ..observability import RuntimeMetrics
from ..permissions import ToolConfirmer
from ..reliability import RetryingChatModel
from ..tools import ToolRegistry
from ..tools.update_plan import PlanUpdater
from .contracts import BusinessContext


@dataclass(frozen=True)
class RuntimeDependencies:
    model: RetryingChatModel
    metrics: RuntimeMetrics
    conversation: Conversation
    sessions: SessionCoordinator
    context_manager: ContextManager
    confirmer: ToolConfirmer
    journal: RunJournal
    logger: logging.Logger
    build_tools: Callable[[PlanUpdater], ToolRegistry]
    business: BusinessContext | None
    profile_id: str
    max_steps: int
    max_finalization_steps: int
    clock: Callable[[], datetime]
