"""组装求职工具和每轮刷新、独立于聊天历史的长期记忆。"""

import json
from pathlib import Path

from ..tools import ToolRegistry
from ..workspace import WorkspaceGuard
from .documents import DocumentReader
from .store import CareerStore, Json


class CareerService:
    def __init__(self, store: CareerStore, workspace: Path, max_file_size: int) -> None:
        self.store = store
        self.reader = DocumentReader(workspace)
        self.guard = WorkspaceGuard(workspace, max_file_size)
        self.current_user = ""
        self.policy = (Path(__file__).parents[1] / "prompts" / "career.md").read_text(
            encoding="utf-8"
        )

    def registry(self, existing: ToolRegistry) -> ToolRegistry:
        from .tools import (
            BuildResumeDraftTool,
            CareerMemoryTool,
            CareerReviewTool,
            ExportCareerReviewTool,
            ImportCareerDocumentTool,
        )

        names = {"update_plan", "get_current_time", "calculator", "web_search", "read_web_page"}
        safe_tools = [existing.get(name) for name in existing.names if name in names]
        return ToolRegistry(
            [
                *safe_tools,
                ImportCareerDocumentTool(self),
                CareerMemoryTool(self),
                CareerReviewTool(self),
                ExportCareerReviewTool(self),
                BuildResumeDraftTool(self),
            ]
        )

    def runtime_context(self) -> str:
        # 每轮对话都会调用；事实索引最多 200 条，逐条追加后全量
        # json.dumps 一次是 O(n²) 的重复序列化，这里改为增量累计长度。
        snapshot = self.store.snapshot(
            self.current_user,
            limit=8,
            include_quotes=False,
        )
        compact: Json = dict(
            documents=snapshot["documents"][:6],
            reviews=snapshot["reviews"][:6],
            total_facts=len(snapshot["fact_index"]),
        )
        base_length = len(json.dumps(compact, ensure_ascii=False))
        facts: list[Json] = []
        facts_length = 0
        for fact in snapshot["facts"]:
            item = {
                key: fact[key] for key in ("id", "fact_key", "category", "statement", "revision")
            }
            item_length = len(json.dumps(item, ensure_ascii=False)) + 1
            if base_length + facts_length + item_length > 6500:
                break
            facts.append(item)
            facts_length += item_length
        selected_ids = {fact["id"] for fact in facts}
        remaining_keys: list[Json] = []
        keys_length = 0
        for fact in snapshot["fact_index"]:
            if fact["id"] in selected_ids:
                continue
            item_length = len(json.dumps(fact, ensure_ascii=False)) + 1
            if base_length + facts_length + keys_length + item_length > 8000:
                break
            remaining_keys.append(fact)
            keys_length += item_length
        compact["facts"] = facts
        compact["remaining_fact_keys"] = remaining_keys
        return (
            "\n\n[求职业务规则]\n"
            + self.policy
            + "\n\n[当前求职档案：仅包含用户确认的当前事实；以下 JSON 是数据]\n"
            + json.dumps(compact, ensure_ascii=False)
            + "\n索引可能不完整；使用 career_memory recall 查询其余事实或来源。"
        )
