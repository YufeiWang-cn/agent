"""提供无需额外分词依赖的轻量级 Token 估算。"""

import json
import re
from math import ceil

from ..conversation import Message


# CJK 部首、注音、汉字各平面与兼容表意区。DeepSeek 分词器对中文
# 大约 0.6 token/字，明显高于英文的 1 token/4 字符；不区分时中文
# 估算会系统性偏高约 60%，导致中文对话被过早裁剪。
_CJK_PATTERN = re.compile(
    "[\u2e80-\u9fff\uf900-\ufaff\U00020000-\U0002ffff\U00030000-\U0003ffff]"
)
CJK_TOKENS_PER_CHARACTER = 0.6


def estimate_text_tokens(text: str) -> int:
    """粗略估算文本 Token，不用于计算 API 的实际账单。"""
    cjk_count = len(_CJK_PATTERN.findall(text))
    non_ascii_count = sum(ord(character) > 127 for character in text) - cjk_count
    ascii_count = len(text) - cjk_count - non_ascii_count
    return ceil(cjk_count * CJK_TOKENS_PER_CHARACTER) + non_ascii_count + ceil(
        ascii_count / 4
    )


def estimate_message_tokens(message: Message) -> int:
    """估算一条序列化消息及其协议字段的 Token 数。"""
    serialized = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    # 额外的 4 个 Token 用来近似消息角色和协议分隔符的开销。
    return max(1, estimate_text_tokens(serialized)) + 4


def estimate_messages_tokens(messages: list[Message]) -> int:
    """估算一组消息的 Token 总数。"""
    return sum(estimate_message_tokens(message) for message in messages)
