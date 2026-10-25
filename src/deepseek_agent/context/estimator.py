"""提供无需额外分词依赖的轻量级 Token 估算。"""

import json
import re
import threading
from collections import OrderedDict
from math import ceil

from ..conversation import Message


# CJK 部首、注音、汉字各平面与兼容表意区。DeepSeek 分词器对中文
# 大约 0.6 token/字，明显高于英文的 1 token/4 字符；不区分时中文
# 估算会系统性偏高约 60%，导致中文对话被过早裁剪。
_CJK_PATTERN = re.compile(
    "[\u2e80-\u9fff\uf900-\ufaff\U00020000-\U0002ffff\U00030000-\U0003ffff]"
)
CJK_TOKENS_PER_CHARACTER = 0.6

# 消息一旦进入会话就不会再被原地修改，同一对象在每次模型步骤中
# 都会被重复估算（上下文裁剪、总量统计、重试层各一遍）。按对象
# 身份缓存估算结果，把重复的全量 json.dumps + 逐字符统计变成查表。
# 缓存同时持有消息引用：对象存活期间 id 不会复用，双重校验
# ``cached is message`` 再防御一次对象被复用的极端情况。
_MESSAGE_CACHE_LIMIT = 4096
_message_token_cache: OrderedDict[int, tuple[Message, int]] = OrderedDict()
_message_token_cache_lock = threading.Lock()


def _estimate_serialized_tokens(serialized: str) -> int:
    cjk_count = len(_CJK_PATTERN.findall(serialized))
    non_ascii_count = sum(ord(character) > 127 for character in serialized) - cjk_count
    ascii_count = len(serialized) - cjk_count - non_ascii_count
    return ceil(cjk_count * CJK_TOKENS_PER_CHARACTER) + non_ascii_count + ceil(
        ascii_count / 4
    )


def estimate_text_tokens(text: str) -> int:
    """粗略估算文本 Token，不用于计算 API 的实际账单。"""
    return _estimate_serialized_tokens(text)


def estimate_message_tokens(message: Message) -> int:
    """估算一条序列化消息及其协议字段的 Token 数。"""
    key = id(message)
    with _message_token_cache_lock:
        cached = _message_token_cache.get(key)
        if cached is not None and cached[0] is message:
            _message_token_cache.move_to_end(key)
            return cached[1]
    serialized = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    # 额外的 4 个 Token 用来近似消息角色和协议分隔符的开销。
    tokens = max(1, _estimate_serialized_tokens(serialized)) + 4
    with _message_token_cache_lock:
        _message_token_cache[key] = (message, tokens)
        if len(_message_token_cache) > _MESSAGE_CACHE_LIMIT:
            _message_token_cache.popitem(last=False)
    return tokens


def estimate_messages_tokens(messages: list[Message]) -> int:
    """估算一组消息的 Token 总数。"""
    return sum(estimate_message_tokens(message) for message in messages)


def clear_estimation_cache() -> None:
    """清空按消息缓存（测试或内存紧张时使用）。"""
    with _message_token_cache_lock:
        _message_token_cache.clear()
