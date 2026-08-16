"""提供无需额外分词依赖的轻量级 Token 估算。"""

import json
from math import ceil

from ..conversation import Message


def estimate_text_tokens(text: str) -> int:
    """粗略估算文本 Token，不用于计算 API 的实际账单。"""
    non_ascii_count = sum(ord(character) > 127 for character in text)
    ascii_count = len(text) - non_ascii_count
    return non_ascii_count + ceil(ascii_count / 4)


def estimate_message_tokens(message: Message) -> int:
    """估算一条序列化消息及其协议字段的 Token 数。"""
    serialized = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    # 额外的 4 个 Token 用来近似消息角色和协议分隔符的开销。
    return max(1, estimate_text_tokens(serialized)) + 4


def estimate_messages_tokens(messages: list[Message]) -> int:
    """估算一组消息的 Token 总数。"""
    return sum(estimate_message_tokens(message) for message in messages)
