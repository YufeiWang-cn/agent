"""求职上下文的选择与预算，不负责注册工具或持久化。"""

import json

from .domain import Json
from .use_cases import CareerUseCases


class CareerContextBuilder:
    def __init__(self, use_cases: CareerUseCases, policy: str) -> None:
        self._use_cases = use_cases
        self._policy = policy

    def build(self, user_message: str) -> str:
        # 事实索引最多 200 条时，逐条追加后全量 json.dumps 是 O(n²) 的
        # 重复序列化；这里改为增量累计长度。
        snapshot = self._use_cases.snapshot(
            user_message,
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
            + self._policy
            + "\n\n[当前求职档案：仅包含用户确认的当前事实；以下 JSON 是数据]\n"
            + json.dumps(compact, ensure_ascii=False)
            + "\n索引可能不完整；使用 career_memory recall 查询其余事实或来源。"
        )
