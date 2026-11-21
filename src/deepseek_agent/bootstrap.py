"""本地应用的唯一装配入口：选择模型、存储、工具和业务扩展。"""

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .config import Settings
from .career.service import CareerService
from .career.use_cases import CareerUseCases
from .infrastructure.sqlite_career import SqliteCareerRepository
from .context import ContextManager
from .conversation import Conversation
from .journal import RunJournal
from .memory import JsonProjectStore, JsonSessionStore, SessionCoordinator
from .models import ChatModel, DeepSeekModel
from .observability import RuntimeMetrics
from .permissions import ConsoleToolConfirmer, ToolConfirmer
from .reliability import RetryingChatModel, RetryPolicy
from .runtime.dependencies import RuntimeDependencies
from .tools import ToolRegistry, build_default_registry
from .tools.update_plan import PlanUpdater


def assemble_runtime(
    settings: Settings,
    *,
    project_root: Path,
    clock: Callable[[], datetime],
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
) -> RuntimeDependencies:
    effective_logger = logger or logging.getLogger(__name__)
    effective_metrics = metrics or RuntimeMetrics()
    wrapped_model = RetryingChatModel(
        model or DeepSeekModel(settings),
        retry_policy or RetryPolicy(settings.max_retries, settings.retry_base_delay),
        effective_metrics,
        logger=logger,
    )
    career_enabled = settings.agent_mode == "career"
    career_root = (
        (project_root / "data" if session_store is None else session_store.directory.parent)
        / "career"
        / settings.career_profile_id
    )
    sessions = session_store or JsonSessionStore(
        career_root / "sessions" if career_enabled else project_root / "data/sessions"
    )
    journal_directory = (
        (career_root / "runs" if career_enabled else project_root / "data/runs")
        if session_store is None
        else sessions.directory / ".runs"
    )
    journal = run_journal or RunJournal(journal_directory)
    projects = project_store or JsonProjectStore(
        career_root / "projects.json" if career_enabled else project_root / "data/projects.json"
    )
    conversation = Conversation(settings.system_prompt)
    coordinator = SessionCoordinator(conversation, sessions, projects)
    business = (
        CareerService(
            CareerUseCases(SqliteCareerRepository(career_root / "profile.sqlite3")),
            settings.workspace_root,
            settings.max_file_size,
        )
        if career_enabled
        else None
    )

    def build_tools(plan_updater: PlanUpdater) -> ToolRegistry:
        registry = (
            tools
            if tools is not None
            else build_default_registry(
                settings.workspace_root,
                settings.max_file_size,
                settings.command_timeout,
                settings.max_command_output,
                plan_updater=plan_updater,
                command_execution_mode=settings.command_execution_mode,
                command_container_image=settings.command_container_image,
                web_search_api_key=settings.tavily_api_key,
                web_search_timeout=settings.web_search_timeout,
                web_search_max_results=settings.web_search_max_results,
                web_search_min_score=settings.web_search_min_score,
                web_search_auto_calls_per_turn=(settings.web_search_auto_calls_per_turn),
                web_search_default_scope=settings.web_search_default_scope,
                web_search_domestic_results=settings.web_search_domestic_results,
                web_search_international_results=(settings.web_search_international_results),
                web_search_domestic_domains=(settings.web_search_domestic_domains),
                web_search_international_domains=(settings.web_search_international_domains),
                web_search_excluded_domains=(settings.web_search_excluded_domains),
                web_page_timeout=settings.web_page_timeout,
                web_page_max_pages_per_call=(settings.web_page_max_pages_per_call),
                web_page_auto_pages_per_turn=(settings.web_page_auto_pages_per_turn),
                web_page_chunks_per_source=(settings.web_page_chunks_per_source),
                web_page_extract_depth=settings.web_page_extract_depth,
                web_page_max_content_chars=(settings.web_page_max_content_chars),
            )
        )
        return business.registry(registry) if business is not None else registry

    return RuntimeDependencies(
        model=wrapped_model,
        metrics=effective_metrics,
        conversation=conversation,
        sessions=coordinator,
        context_manager=context_manager or ContextManager(settings.max_context_tokens),
        confirmer=confirmer if confirmer is not None else ConsoleToolConfirmer(),
        journal=journal,
        logger=effective_logger,
        build_tools=build_tools,
        business=business,
        profile_id=settings.career_profile_id,
        max_steps=settings.max_agent_steps,
        max_finalization_steps=settings.max_finalization_steps,
        clock=clock,
    )
